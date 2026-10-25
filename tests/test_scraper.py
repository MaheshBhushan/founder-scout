from founder_scout.scout import page_emails
from founder_scout.scraper import decode_cfemail, html_to_markdown


def cf_encode(email: str, key: int = 0x42) -> str:
    return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in email)


def test_decodes_cloudflare_protected_email():
    assert decode_cfemail(cf_encode("ada@startup.io")) == "ada@startup.io"


def test_mailto_and_cfemail_reach_page_text():
    html = f"""<html><body><h2>Team</h2>
    <p>Ada Lovelace, CEO <a href="mailto:ada@startup.io?subject=hi">Email Ada</a></p>
    <p>Bob <span class="__cf_email__" data-cfemail="{cf_encode('bob@startup.io')}">[email protected]</span></p>
    <a href="/cdn-cgi/l/email-protection#{cf_encode('carol@startup.io')}">Carol</a>
    <script>var x = "junk@tracker.com";</script></body></html>"""
    text = html_to_markdown(html, "https://startup.io/team")
    assert "## Team" in text
    assert page_emails(text) == ["ada@startup.io", "bob@startup.io", "carol@startup.io"]


def test_links_are_absolute():
    text = html_to_markdown('<a href="/about">About us</a>', "https://startup.io/team")
    assert "[About us](https://startup.io/about)" in text


def test_icon_only_profile_links_keep_their_url():
    text = html_to_markdown('<a href="https://www.linkedin.com/in/ada"><svg></svg></a>', "https://startup.io/team")
    assert "(https://www.linkedin.com/in/ada)" in text
