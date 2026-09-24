"""tests/test_send_delivery.py —— dws 发送响应判定（send_accepted）。

背景（2026-09-25）：用户连续 6 次反馈「文件发不出来」（任务 #236-#241），而审计
一路记 send_file 成功、日志里查不到任何失败痕迹。根因之一是判定过松：

    ok = bool(r) and "_exc" not in r and r.get("success") is not False

Dws.run() 的失败返回形态是 {"_timeout":True} / {"_error":…} / {"raw":…} /
{"error":{…}}，这些**全部**能通过上面那个判定——因为真实成功响应里 success 嵌在
data 下、顶层没有该键，于是 r.get("success") 恒为 None、`is not False` 恒真。

另一层：dws 文件发送是**异步**的（outcome=pending、state=processing），
data.success=true 只代表「请求被受理」，真到没到要查 query-send-status 的
sendStatus。故 ok=True 只是受理，投递确认由调用方轮询 send_status 完成。
"""
import pytest

from app.ingress_dingtalk import send_accepted


REAL_OK = {
    "ok": True, "outcome": "pending",
    "data": {"arguments": [], "errorCode": None, "errorMsg": None,
             "result": {"openTaskId": "TID123"}, "success": True},
    "meta": {"operation": {"id": "TID123", "state": "processing"}},
}


def test_real_success_response_accepted_with_task_id():
    ok, tid, err = send_accepted(REAL_OK)
    assert ok is True and tid == "TID123" and err == ""


@pytest.mark.parametrize("r,why", [
    ({"_timeout": True}, "超时"),
    ({"_error": "dws 崩了"}, "异常"),
    ({"_exc": "TypeError(...)"}, "调用侧异常"),
    ({"raw": "command not found"}, "stdout 非 JSON"),
    ({"error": {"message": "business error: success=false", "code": 1}}, "dws 业务错误"),
    ({"error": "plain string error"}, "dws 业务错误(字符串)"),
    ({"ok": True, "data": {"errorCode": "Forbidden", "errorMsg": "无权限"}}, "errorCode"),
    ({"ok": True, "data": {"success": False, "errorMsg": "文件不存在"}}, "data.success=False"),
    ({"ok": False, "data": {"success": True}}, "顶层 ok=False"),
    ({}, "空 dict"),
    (None, "None"),
    ("not a dict", "非 dict"),
    ({"success": True}, "只有顶层 success（旧判定会误判为成功）"),
])
def test_failure_shapes_all_rejected(r, why):
    """旧判定把这些全当成功；新判定必须全部识别为失败。"""
    ok, tid, err = send_accepted(r)
    assert ok is False, f"{why} 应判失败，实际 ok={ok}"
    assert err, f"{why} 应给出错误摘要"


def test_dry_run_treated_as_handled():
    assert send_accepted({"dry": True})[0] is True


def test_ok_without_task_id_still_accepted():
    """测试桩与部分同步响应只回 {"ok": True}：受理成立、无 taskId 可查投递。"""
    ok, tid, err = send_accepted({"ok": True})
    assert ok is True and tid == "" and err == ""


def test_response_with_no_markers_rejected():
    """既无 ok 也无 data.success 的响应不得当成成功（旧判定的核心漏洞）。"""
    ok, _, err = send_accepted({"processQueryKey": "abc", "result": {}})
    assert ok is False and "无 ok/success" in err
