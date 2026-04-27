"""HttpFetcher wrapper 테스트.

실제 Scrapling Fetcher는 호출하지 않고, _detect_blocked / _safe_json 같은
순수 로직만 검증한다. 통합 테스트는 별도 marker로.
"""

from app.collectors.fetchers.http import HttpFetcher


def test_detect_blocked_on_403():
    assert HttpFetcher._detect_blocked(403, {}, "")


def test_detect_blocked_on_503():
    assert HttpFetcher._detect_blocked(503, {}, "")


def test_cf_ray_alone_does_not_mark_blocked():
    # cf-ray는 Cloudflare 경유 신호일 뿐 정상 응답에도 존재.
    assert not HttpFetcher._detect_blocked(200, {"CF-Ray": "abc"}, '{"recruitData":[]}')


def test_detect_blocked_on_just_a_moment_body():
    assert HttpFetcher._detect_blocked(200, {}, "<html>Just a moment...</html>")


def test_detect_blocked_on_captcha_phrase():
    assert HttpFetcher._detect_blocked(200, {}, "Please complete the captcha to continue")


def test_not_blocked_on_word_captcha_substring():
    # "captcha" 단순 substring으로는 false positive 안 나야 함.
    assert not HttpFetcher._detect_blocked(200, {}, '{"NotCaptchaField": "value"}')


def test_not_blocked_for_normal_response():
    assert not HttpFetcher._detect_blocked(200, {"Server": "nginx"}, "<html>ok</html>")


def test_safe_json_returns_parsed():
    assert HttpFetcher._safe_json('{"a": 1}') == {"a": 1}


def test_safe_json_returns_none_for_empty_text():
    assert HttpFetcher._safe_json("") is None


def test_safe_json_returns_none_for_invalid_json():
    assert HttpFetcher._safe_json("<html>not json</html>") is None


def test_detect_blocked_on_200_empty_body():
    assert HttpFetcher._detect_blocked(200, {}, "")


def test_with_query_appends_params():
    url = HttpFetcher._with_query("https://x.test/api", {"a": 1, "b": "y"})
    assert url.startswith("https://x.test/api?")
    assert "a=1" in url and "b=y" in url
