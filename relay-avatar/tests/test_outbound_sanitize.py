"""tests/test_outbound_sanitize.py —— 出站文本控制字符清洗（strip_ctrl/md_hardbreak）。

背景（2026-10-01）：孔德光在群内 @鲁大师 提问，任务 #388 升级 relay 成 #389 后，
分身每 31 秒刷一次 `dws error: msgContent contains invalid control characters`
（category=validation, code=3），从 17:10:09 一直到 17:23:35 共 27 次，直到 #389
于 17:24:02 到达终态、卡片被 _card_final 弹出才停。

根因：**真实 @ 提及在钉钉事件载荷里就是用 \\x07(BEL) 做分隔的**。实测该条消息原文：

    '@鲁大师\\x07今天净头寸策略是什么？从三层净头寸策略结合实际市场数据和内部数据，
     给出净头寸调整的方向和每个腿上的关键举措。'

传播链：content → desc → card_hint（main.py 取首行前 40 字）→ _card_frame →
card_update --content → 钉钉整条拒收。而 _card_beat 的 `except Exception: pass`
只吞掉 Python 异常，**dws CLI 自己把错误打到 stderr**，照样进 journal；
card_progress_sec=30 的进度节流又保证它每轮重试，于是形成刷屏循环。

收敛点：出站文本只有 send_text 与 card_update 两条路，二者都经过 md_hardbreak
（前者在 main.py:_notify 显式调用，后者在 Dws.card_update 内部调用），
card_create 不带正文，故在 md_hardbreak 里清洗即全覆盖。
"""
import pytest

from app.ingress_dingtalk import md_hardbreak, strip_ctrl

# 2026-10-01 事故原文（孔德光群内 @鲁大师，\x07 是钉钉真实 @ 提及的分隔符）
REAL_INCIDENT = ("@鲁大师\x07今天净头寸策略是什么？从三层净头寸策略结合实际市场数据"
                 "和内部数据，给出净头寸调整的方向和每个腿上的关键举措。")


# ── strip_ctrl ──

def test_strip_ctrl_removes_bel_from_real_incident():
    """事故原文里的 \x07 必须被剔除，其余字符一字不动。"""
    out = strip_ctrl(REAL_INCIDENT)
    assert "\x07" not in out
    assert out == REAL_INCIDENT.replace("\x07", "")
    assert out.startswith("@鲁大师今天净头寸策略是什么？")


def test_strip_ctrl_keeps_newline_and_tab():
    """\n 与 \t 是 Markdown 排版必需，不得误删。"""
    assert strip_ctrl("a\nb\tc") == "a\nb\tc"
    assert strip_ctrl("第一行\n第二行\n\n第四行") == "第一行\n第二行\n\n第四行"


def test_strip_ctrl_removes_all_c0_and_del():
    """C0 全集（除 \\n\\t）与 DEL 都要清掉；\\n\\t 必须留下。"""
    src = "".join(chr(i) for i in range(32)) + "正文" + "\x7f"
    out = strip_ctrl(src)
    # 原串里 \t(\x09) 与 \n(\x0a) 被保留，其余 C0 全清
    assert out == "\t\n正文"
    for i in range(32):
        if chr(i) not in "\n\t":
            assert chr(i) not in out
    assert "\x7f" not in out
    assert "\n" in out and "\t" in out


def test_strip_ctrl_keeps_cjk_emoji_and_nbsp():
    """中文、emoji、全角标点、不换行空格都必须原样保留（业务正文常见）。"""
    s = "🍂 鲁大师办好了 ✅　全角空格\u00a0不换行空格—破折号…"
    assert strip_ctrl(s) == s


def test_strip_ctrl_empty_and_none():
    assert strip_ctrl("") == ""
    assert strip_ctrl(None) == ""


def test_strip_ctrl_idempotent():
    once = strip_ctrl(REAL_INCIDENT)
    assert strip_ctrl(once) == once


# ── md_hardbreak 集成（出站唯一收敛点）──

def test_md_hardbreak_strips_ctrl():
    """md_hardbreak 必须顺带清洗——card_update 只经过它，不再单独调 strip_ctrl。"""
    out = md_hardbreak(REAL_INCIDENT)
    assert "\x07" not in out
    assert "今天净头寸策略是什么" in out


def test_md_hardbreak_ctrl_only_content_becomes_empty():
    assert md_hardbreak("\x07\x00\x1b") == ""


def test_md_hardbreak_still_adds_hard_break():
    """清洗不得破坏原有硬换行行为。

    原语义（实测锁定，非推测）：只有当**下一行 strip 后非空**时才给当前行补两个
    空格（`nxt.strip() != ""`）。故下一行是空串或已到末尾时都不补。
    """
    assert md_hardbreak("甲\n乙") == "甲  \n乙"
    assert md_hardbreak("甲\n乙\n丙") == "甲  \n乙  \n丙"
    assert md_hardbreak("甲\n") == "甲\n"            # 下一行是空串 → 不补
    assert md_hardbreak("甲\n\n乙") == "甲\n\n乙"      # 同上
    assert md_hardbreak("甲\n乙\n") == "甲  \n乙\n"
    assert md_hardbreak("单行") == "单行"


def test_md_hardbreak_preserves_code_block_and_table():
    """代码块与表格行不补空格（原有行为），且其中的控制字符同样被清。"""
    src = "```bash\nls\x07 -la\n```\n| a | b |"
    out = md_hardbreak(src)
    assert "\x07" not in out
    assert "ls -la" in out
    assert "| a | b |" in out
    # 代码块内不补硬换行
    assert "ls -la\n" in out


def test_md_hardbreak_none_safe():
    assert md_hardbreak(None) == ""
    assert md_hardbreak("") == ""


# ── 卡片正文（事故现场的实际拼装路径）──

def test_card_frame_shape_survives_sanitization():
    """_card_frame 的产物经 md_hardbreak 后必须干净且结构完整。

    复刻 main.py:_card_frame 的拼装形态（card_hint 取 desc 首行前 40 字），
    确保清洗发生在正确的位置、不会把卡片骨架也削掉。
    """
    desc = REAL_INCIDENT
    card_hint = (desc.splitlines() or [""])[0][:40]
    assert "\x07" in card_hint              # 清洗前确实带毒
    frame = (f"🍂 **鲁大师正在处理…**\n\n"
             f"> 📥 已收到：{card_hint}\n> ⏳ 正在为你办…（已用 31s）")
    out = md_hardbreak(frame)
    assert "\x07" not in out
    assert out.startswith("🍂 **鲁大师正在处理…**")
    assert "> 📥 已收到：@鲁大师今天净头寸策略是什么" in out
    assert "> ⏳ 正在为你办…（已用 31s）" in out


@pytest.mark.parametrize("bad", ["\x00", "\x07", "\x1b", "\x7f", "\x0b", "\x0c"])
def test_no_single_control_char_survives(bad):
    """逐个参数化验证：任何一个控制字符都不得出现在出站文本里。"""
    assert bad not in md_hardbreak(f"前文{bad}后文")
    assert bad not in strip_ctrl(f"前文{bad}后文")
