"""Contract tests for the image error map.

Run: python test_error_map.py -v

Every case here is a shape a real vendor produced or documents. The point is
not coverage of lines; it is that the *same* semantic arriving on a different
status (or inside a 200) lands on the same canonical, and that nothing the
caller sees contains upstream text.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

# The classifier and its map are vendored into the app package; this suite
# must run against *our* copy, not the skill's, or a local rule edit would
# ship untested.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import gateway_errors as ge  # noqa: E402


def c(provider, status, payload=None, body_text="", **kw):
    return ge.classify(provider, status, payload, body_text, **kw)


class Judgment(unittest.TestCase):
    # --- OpenAI ------------------------------------------------------------
    def test_openai_moderation(self):
        d = c("openai", 400, {"error": {
            "code": "moderation_blocked", "type": "invalid_request_error",
            "message": "Your request was rejected as a result of our safety system."}})
        self.assertEqual(d.canonical, "content_policy_violation")
        self.assertEqual(d.http, 400)
        self.assertEqual(ge.retry_policy(d)["rotate"], False, "rotating keys to dodge moderation flags the pool")

    def test_openai_invalid_image_format(self):
        d = c("openai", 400, {"error": {"code": "invalid_image_format", "message": "must be png/jpeg/webp"}})
        self.assertEqual(d.canonical, "invalid_image")

    def test_openai_user_error_by_type_only(self):
        # OpenAI's stable discriminator is error.code, but the type alone must
        # still be non-retryable rather than unknown.
        d = c("openai", 400, {"error": {"type": "image_generation_user_error", "message": "bad size"}})
        self.assertEqual(d.canonical, "invalid_request_error")
        self.assertEqual(ge.retry_policy(d)["rotate"], False)

    def test_openai_billing_hard_limit_is_quota_on_429(self):
        d = c("openai", 400, {"error": {"code": "billing_hard_limit_reached", "message": "limit"}})
        self.assertEqual(d.canonical, "insufficient_quota")
        self.assertEqual(d.http, 429, "outbound contract follows OpenAI: quota is a 429")

    def test_openai_rate_limit_has_retry_after(self):
        d = c("openai", 429, {"error": {"code": "rate_limit_exceeded", "message": "slow down"}})
        self.assertEqual(d.canonical, "rate_limit_exceeded")
        self.assertEqual(d.retry_after, 30)

    def test_openai_quota_has_no_retry_after(self):
        d = c("openai", 429, {"error": {"code": "insufficient_quota", "message": "no credit"}})
        self.assertEqual(d.canonical, "insufficient_quota")
        self.assertIsNone(d.retry_after, "a terminal state must not suggest waiting")

    def test_partial_delivery_is_not_an_error(self):
        d = c("openai", 200, {"data": [{"url": "https://cdn/1.png"}, {"url": "https://cdn/2.png"}]}, n=4)
        self.assertTrue(d.partial)
        with self.assertRaises(ValueError):
            ge.to_caller(d)
        with self.assertRaises(ValueError):
            ge.GatewayError(d)

    def test_200_with_error_member_is_failure(self):
        d = c("openai", 200, {"error": {"code": "upstream_failed", "message": "boom"}})
        self.assertEqual(d.canonical, "api_error")
        self.assertEqual(d.http, 500)

    def test_200_empty_data_is_generation_failed(self):
        d = c("openai", 200, {"data": [], "usage": {"total_tokens": 0}})
        self.assertEqual(d.canonical, "generation_failed")

    def test_clean_success_returns_none(self):
        self.assertIsNone(c("openai", 200, {"data": [{"b64_json": "AAAA"}]}, n=1))

    def test_empty_base64_placeholder_is_not_an_image(self):
        d = c("openai", 200, {"data": [{"b64_json": ""}]}, n=1)
        self.assertEqual(d.canonical, "generation_failed")

    # --- 方舟 / Seedream ---------------------------------------------------
    def test_ark_reference_download_timeout(self):
        d = c("ark", 400, {"error": {
            "code": "InvalidParameter",
            "message": "The parameter `image` specified in the request are not valid: "
                       "Timeout while downloading url=https://slow.example/a.jpg"}})
        self.assertEqual(d.canonical, "invalid_image", "ARK's own 5s fetch cap is an input-image problem")

    def test_ark_real_person(self):
        d = c("ark", 400, {"error": {
            "code": "InputImageSensitiveContentDetected.PrivacyInformation",
            "message": "The request failed because the input image may contain real person."}})
        self.assertEqual(d.canonical, "content_policy_violation")

    def test_ark_overloaded_is_503_outbound(self):
        d = c("ark", 429, {"error": {"code": "ServerOverloaded", "message": "busy"}})
        self.assertEqual(d.canonical, "server_is_overloaded")
        self.assertEqual(d.http, 503)

    def test_ark_quota_vs_rate_on_same_status(self):
        quota = c("ark", 429, {"error": {"code": "QuotaExceeded", "message": "free quota"}})
        rate = c("ark", 429, {"error": {"code": "RateLimitExceeded.EndpointRPMExceeded", "message": "rpm"}})
        self.assertEqual(quota.canonical, "insufficient_quota")
        self.assertEqual(rate.canonical, "rate_limit_exceeded")

    def test_ark_overdue_hides_in_403(self):
        d = c("ark", 403, {"error": {"code": "AccountOverdueError", "message": "overdue"}})
        self.assertEqual(d.canonical, "insufficient_quota")
        self.assertEqual(d.http, 429)

    # --- 百炼 / qwen -------------------------------------------------------
    def test_qwen_poll_moderation(self):
        d = c("qwen", 200, {"output": {"task_status": "FAILED", "code": "DataInspectionFailed",
                                      "message": "内容安全警告"}}, stage="poll")
        self.assertEqual(d.canonical, "content_policy_violation")

    def test_qwen_poll_internal_failure(self):
        d = c("qwen", 200, {"output": {"task_status": "FAILED", "code": "InternalError",
                                      "message": "inference error"}}, stage="poll")
        self.assertEqual(d.canonical, "generation_failed")
        self.assertEqual(d.billed, "yes")

    def test_qwen_poll_unmapped_code_stays_a_generation_failure(self):
        d = c("qwen", 200, {"output": {"task_status": "FAILED", "code": "SomethingNew"}}, stage="poll")
        self.assertEqual(d.canonical, "generation_failed", "do not fall through to unknown here")

    def test_qwen_partial_metrics_is_partial(self):
        d = c("qwen", 200, {"output": {
            "task_status": "SUCCEEDED",
            "task_metrics": {"TOTAL": 4, "SUCCEEDED": 3, "FAILED": 1},
            "results": [{"url": "https://cdn/1.png"}, {"url": "https://cdn/2.png"}, {"url": "https://cdn/3.png"}]}},
            n=4, stage="poll")
        self.assertTrue(d.partial)

    def test_qwen_submit_throttle(self):
        d = c("qwen", 429, {"code": "Throttling.BurstRate", "message": "burst"})
        self.assertEqual(d.canonical, "rate_limit_exceeded")

    def test_qwen_web_quota_wordings(self):
        d = c("qwen", 200, {"success": False, "data": {
            "code": "RateLimited", "details": "当前账号额度已用完，请明天再试"}})
        self.assertEqual(d.canonical, "insufficient_quota",
                         "RateLimited is overloaded; only the wording says the identity is spent")

    def test_qwen_web_transient_under_same_code(self):
        d = c("qwen", 200, {"success": False, "data": {
            "code": "RateLimited", "details": "目前服务访问量较大，请稍后重试"}})
        self.assertEqual(d.canonical, "server_is_overloaded")

    def test_qwen_x5sec_is_not_retried(self):
        d = c("qwen", 200, {"ret": ["FAIL_SYS_USER_VALIDATE::RGV587"], "data": {}})
        self.assertEqual(d.canonical, "permission_denied")
        policy = ge.retry_policy(d)
        self.assertEqual(policy["rotate"], False)
        self.assertGreaterEqual(policy["cooldown_s"], 900, "hammering deepens the block")

    # --- 混元 --------------------------------------------------------------
    def test_hunyuan_job_moderation(self):
        d = c("hunyuan", 200, {"Response": {"JobStatusCode": "4",
                                            "JobErrorCode": "FailedOperation.GenerateImageFailed",
                                            "JobErrorMsg": "生成图片审核不通过"}}, stage="poll")
        self.assertEqual(d.canonical, "content_policy_violation")
        self.assertEqual(d.raw.job_status, "FAILED")

    def test_hunyuan_job_not_exist(self):
        d = c("hunyuan", 200, {"Response": {"JobStatusCode": "4",
                                            "JobErrorCode": "FailedOperation.JobNotExist"}}, stage="poll")
        self.assertEqual(d.canonical, "task_not_found")
        self.assertEqual(d.http, 404)

    def test_hunyuan_failed_without_code_is_still_a_failure(self):
        d = c("hunyuan", 200, {"Response": {"JobStatusCode": "4"}}, stage="poll")
        self.assertEqual(d.canonical, "generation_failed")

    def test_hunyuan_input_download_error(self):
        d = c("hunyuan", 200, {"Response": {"JobStatusCode": "4",
                                            "JobErrorCode": "FailedOperation.DownloadError",
                                            "JobErrorMsg": "文件下载失败"}}, stage="poll")
        self.assertEqual(d.canonical, "invalid_image")

    def test_hunyuan_completed_without_image_is_failure(self):
        d = c("hunyuan", 200, {"Response": {"JobStatusCode": "5", "ResultImage": [],
                                            "ResultDetails": []}}, stage="poll")
        self.assertEqual(d.canonical, "generation_failed", "a 200 with no picture is not a success")

    # --- MiniMax -----------------------------------------------------------
    def test_minimax_quota_on_200(self):
        d = c("minimax", 200, {"base_resp": {"status_code": 1008, "status_msg": "insufficient balance"}})
        self.assertEqual(d.canonical, "insufficient_quota")
        self.assertEqual(d.http, 429)

    def test_minimax_input_sensitive(self):
        d = c("minimax", 200, {"base_resp": {"status_code": 1026, "status_msg": "input new_sensitive"}})
        self.assertEqual(d.canonical, "content_policy_violation")

    def test_minimax_rate_and_auth(self):
        self.assertEqual(c("minimax", 200, {"base_resp": {"status_code": 1002}}).canonical,
                         "rate_limit_exceeded")
        self.assertEqual(c("minimax", 200, {"base_resp": {"status_code": 1004}}).canonical,
                         "authentication_error")

    def test_minimax_success_is_not_an_error(self):
        self.assertIsNone(c("minimax", 200, {
            "base_resp": {"status_code": 0, "status_msg": "success"},
            "data": {"image_urls": ["https://cdn/1.png"]}}, n=1))

    # --- Gemini ------------------------------------------------------------
    def test_gemini_prompt_block(self):
        d = c("google", 200, {"promptFeedback": {"blockReason": "SAFETY"}})
        self.assertEqual(d.canonical, "content_policy_violation")

    def test_gemini_output_block(self):
        d = c("google", 200, {"candidates": [{"finishReason": "IMAGE_SAFETY",
                                             "content": {"parts": [{"text": "refused"}]}}]})
        self.assertEqual(d.canonical, "content_policy_violation")

    def test_gemini_no_image(self):
        d = c("google", 200, {"candidates": [{"finishReason": "NO_IMAGE", "content": {"parts": []}}]})
        self.assertEqual(d.canonical, "generation_failed")

    def test_gemini_402_vs_429_same_status_string(self):
        body = {"error": {"code": 402, "status": "RESOURCE_EXHAUSTED", "message": "prepaid credits"}}
        self.assertEqual(c("google", 402, body).canonical, "insufficient_quota")
        body["error"]["code"] = 429
        self.assertEqual(c("google", 429, body).canonical, "rate_limit_exceeded",
                         "same gRPC status, different HTTP code, different action")

    def test_gemini_moderation_unavailable_is_retryable(self):
        d = c("google", 502, {"error": {"code": 502, "status": "MODERATION_UNAVAILABLE",
                                       "message": "moderation is unavailable"}})
        self.assertEqual(d.canonical, "api_error")
        self.assertGreater(ge.retry_policy(d)["same_account_retry"], 0)

    # --- fal / Stability ---------------------------------------------------
    def test_fal_queue_failed(self):
        d = c("fal", 200, {"status": "FAILED", "error": "CUDA out of memory"}, stage="poll")
        self.assertEqual(d.canonical, "generation_failed")

    def test_fal_queue_policy_failure(self):
        d = c("fal", 200, {"status": "FAILED", "error": "NSFW content detected"}, stage="poll")
        self.assertEqual(d.canonical, "content_policy_violation")

    def test_fal_422_policy_is_not_a_validation_error(self):
        d = c("fal", 422, {"detail": "the prompt was rejected by our content policy"})
        self.assertEqual(d.canonical, "content_policy_violation")

    def test_stability_content_filtered(self):
        d = c("stability", 200, {"artifacts": [], "finish_reason": "CONTENT_FILTERED"})
        self.assertEqual(d.canonical, "content_policy_violation")

    # --- phase-dependent ------------------------------------------------
    def test_result_404_is_an_expired_link_not_a_missing_model(self):
        d = c("openai", 404, None, stage="result")
        self.assertEqual(d.canonical, "result_unavailable")
        self.assertEqual(ge.retry_policy(d)["rotate"], False,
                         "re-generating to fix a broken link bills a second image")

    def test_result_5xx_is_transient_and_does_not_trip_the_channel(self):
        d = c("openai", 503, None, stage="result")
        self.assertEqual(d.canonical, "api_error")
        policy = ge.retry_policy(d)
        self.assertEqual(policy["same_account_retry"], 1, "a free re-download is allowed")
        self.assertFalse(policy["channel_penalty"], "a bucket hiccup is not a generation channel fault")

    def test_accept_phase_task_id_is_not_an_empty_result(self):
        self.assertIsNone(c("qwen", 200, {"output": {"task_id": "t-1", "task_status": "PENDING"}},
                            stage="accept"))

    def test_poll_5xx_must_not_resubmit(self):
        d = c("qwen", 500, {"message": "boom"}, stage="poll")
        self.assertEqual(d.canonical, "api_error")
        policy = ge.retry_policy(d)
        self.assertEqual(policy["resubmit"], False)
        self.assertEqual(policy["same_account_retry"], 0)

    def test_submit_5xx_may_retry_once(self):
        for provider, payload in (
            ("qwen", {"message": "boom"}),
            ("ark", {"error": {"code": "InternalServiceError"}}),
            ("google", {"error": {"status": "UNAVAILABLE"}}),
        ):
            d = c(provider, 503 if provider == "google" else 500, payload)
            self.assertEqual(ge.retry_policy(d)["same_account_retry"], 1, provider)

    def test_operator_message_has_urls_sanitized(self):
        payload = {"error": {"code": "InvalidParameter",
                             "message": "Timeout while downloading url="
                                        "https://cos.ap-guangzhou.myqcloud.com/a.png?q-ak=AKID1234567890&q-signature=abc"}}
        d = c("ark", 400, payload)
        row = ge.to_operator(d, "req_op00002", payload=payload)
        self.assertIn("?<redacted>", row["raw_message"])
        self.assertNotIn("AKID", row["raw_message"])
        self.assertIn("Timeout while downloading", row["raw_message"])

    def test_html_body_stays_operator_only(self):
        html = "<html><body>502 Bad Gateway from 10.20.30.40</body></html>"
        d = c("openai", 502, None, html)
        self.assertEqual(d.canonical, "api_error")
        self.assertEqual(d.raw.text, html[: ge.RAW_BODY_CAP])
        body = json.dumps(ge.to_caller(d, "req_html0001"), ensure_ascii=False)
        self.assertNotIn("10.20.30.40", body)
        self.assertNotIn("Bad Gateway", body)

    def test_param_is_carried_when_the_vendor_names_it(self):
        d = c("openai", 400, {"error": {"code": "invalid_request_error", "param": "size",
                                       "message": "unsupported size"}})
        self.assertEqual(ge.to_caller(d)["error"].get("param"), "size")


class Contract(unittest.TestCase):
    def test_every_canonical_has_a_policy(self):
        self.assertEqual(set(ge.CANONICAL), set(ge._POLICY))

    def test_every_caller_template_is_clean(self):
        for name, spec in ge.CANONICAL.items():
            if spec.get("not_an_error"):
                continue
            message = str(spec["message"]).format(retry_after=30, request_id="req_abcd1234")
            ge.assert_clean(message)

    def test_no_caller_body_contains_upstream_text(self):
        cases = [
            ("openai", 400, {"error": {"code": "moderation_blocked",
                                      "message": "org-A1b2C3d4 sk-live-9f8a7b6c5d4e balance 0"}}),
            ("ark", 403, {"error": {"code": "AccountOverdueError", "message": "account 2100123456"}}),
            ("hunyuan", 200, {"Response": {"JobStatusCode": "4", "JobErrorCode": "FailedOperation.DownloadError",
                                           "JobErrorMsg": "https://cos.ap-guangzhou.myqcloud.com/a.png?q-ak=AKIDzzzz"}},
             ),
        ]
        for provider, status, payload in cases:
            kw = {"stage": "poll"} if provider == "hunyuan" else {}
            d = c(provider, status, payload, **kw)
            body = json.dumps(ge.to_caller(d, "req_x1"), ensure_ascii=False)
            self.assertEqual(set(json.loads(body)), {"error"})
            ge.assert_clean(body)
            self.assertNotIn("sk-live", body)
            self.assertNotIn("AKID", body)

    def test_operator_record_redacts_signed_urls_and_base64(self):
        payload = {
            "error": {"code": "invalid_image_format", "message": "bad"},
            "debug": {
                "url": "https://bucket.oss-cn-shanghai.aliyuncs.com/a.png?OSSAccessKeyId=AKID12345678&Expires=9",
                "b64": "A" * 600,
            },
        }
        d = c("openai", 400, payload)
        row = ge.to_operator(d, "req_op00001", payload=payload)
        self.assertEqual(row["raw_payload"]["debug"]["url"], "https://bucket.oss-cn-shanghai.aliyuncs.com/a.png?<redacted>")
        self.assertEqual(list(row["raw_payload"]["debug"]["b64"]), ["_long_string", "_sha256"])
        self.assertEqual(row["billed"], "no")

    def test_operator_record_prefers_the_string_code(self):
        payload = {"error": {"code": 402, "status": "RESOURCE_EXHAUSTED", "message": "prepaid"}}
        d = c("google", 402, payload)
        row = ge.to_operator(d, "req_op00003", payload=payload)
        self.assertEqual(row["raw_code"], "RESOURCE_EXHAUSTED")
        self.assertIn("402", row["raw_codes"])

    def test_leak_guard_catches_image_specific_secrets(self):
        self.assertTrue(ge.leaks("see https://x.com/a.png?q-ak=AKID1234567890&q-signature=abc"))
        self.assertTrue(ge.leaks("data:image/png;base64," + "A" * 40))
        self.assertTrue(ge.leaks("key sk-live-1234567890"))
        self.assertFalse(ge.leaks("the upstream link has expired"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
