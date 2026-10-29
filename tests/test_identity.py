import json
import stat
import time

from super_fetch.identity import Identity

UA = {"user-agent": "Mozilla/5.0 Firefox/156.0", "accept-language": "en-US,en;q=0.9"}


def cookie(name, domain, *, path="/", secure=False, expires=None):
    return {
        "name": name,
        "value": f"{name}-value",
        "domain": domain,
        "path": path,
        "secure": secure,
        "expires": time.time() + 3600 if expires is None else expires,
    }


def test_cookies_for_matches_domain_path_and_scheme():
    identity = Identity(None)
    identity.update_cookies(
        [
            cookie("site", ".example.com"),
            cookie("host_only", "www.example.com"),
            cookie("apex_only", "example.com"),
            cookie("docs", ".example.com", path="/docs"),
            cookie("secure", ".example.com", secure=True),
            cookie("other", ".other.com"),
        ]
    )
    assert set(identity.cookies_for("https://www.example.com/docs/a")) == {"site", "host_only", "docs", "secure"}
    assert set(identity.cookies_for("http://example.com/")) == {"site", "apex_only"}
    assert identity.cookies_for("https://notexample.com/") == {}


def test_expired_cookies_are_ignored():
    identity = Identity(None)
    identity.update_cookies(
        [cookie("old", "example.com", expires=time.time() - 1), cookie("session", "example.com", expires=-1)]
    )
    assert set(identity.cookies_for("https://example.com/")) == {"session"}


def test_clearance_needs_cookie_and_recorded_headers():
    identity = Identity(None)
    identity.update_cookies([cookie("cf_clearance", ".example.com"), cookie("consent", "other.com")])
    assert not identity.has_clearance("https://example.com/")  # browser headers not known yet
    identity.record_headers({**UA, "accept": "*/*"})
    assert identity.headers == UA
    assert identity.has_clearance("https://www.example.com/page")
    assert not identity.has_clearance("https://other.com/")


def test_persists_identity_and_long_lived_cookies(tmp_path):
    first = Identity(tmp_path)
    preset = first.preset()
    first.record_headers(UA)
    first.update_cookies([cookie("cf_clearance", "example.com"), cookie("session", "example.com", expires=-1)])

    second = Identity(tmp_path)
    assert second.preset() == preset
    assert second.headers == UA
    assert [c["name"] for c in second.cookies] == ["cf_clearance"]
    assert stat.S_IMODE((tmp_path / "cookies.json").stat().st_mode) == 0o600


def test_saving_keeps_cookies_from_other_processes(tmp_path):
    a, b = Identity(tmp_path), Identity(tmp_path)
    a.update_cookies([cookie("a", "a.com")])
    b.update_cookies([cookie("b", "b.com")])
    a.update_cookies([cookie("a", "a.com"), cookie("a2", "a.com")])
    saved = json.loads((tmp_path / "cookies.json").read_text())
    assert sorted(c["name"] for c in saved) == ["a", "a2", "b"]


def test_reset_forgets_everything(tmp_path):
    identity = Identity(tmp_path)
    identity.preset()
    identity.update_cookies([cookie("cf_clearance", "example.com")])
    identity.reset()
    assert identity.cookies == []
    assert list(tmp_path.iterdir()) == []


def test_preset_for_another_os_starts_a_new_identity(tmp_path):
    first = Identity(tmp_path)
    assert first.preset("windows")["navigator"]["platform"] == "Win32"
    first.record_headers(UA)
    first.update_cookies([cookie("cf_clearance", "example.com")])

    second = Identity(tmp_path)
    assert second.preset() == first.preset("windows")
    assert second.preset("macos")["navigator"]["platform"] == "MacIntel"
    assert second.headers == {}
    assert second.cookies == []
    assert Identity(tmp_path).preset("macos") == second.preset("macos")
