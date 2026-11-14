from founder_scout.scout import html_text, page_emails


def test_strips_button_text_glued_to_address():
    text = "eleanor.wicks@lifeyear.comCopy · t.m@hopper-mobility.comWeitere"
    assert page_emails(text) == ["eleanor.wicks@lifeyear.com", "t.m@hopper-mobility.com"]


def test_keeps_real_long_tlds():
    text = "a@b.design c@d.network e@f.company"
    assert page_emails(text) == ["a@b.design", "c@d.network", "e@f.company"]


def test_direct_html_keeps_addresses_apart_from_neighbouring_elements():
    html = '<p><span>Email:</span><span>a@b.com</span><span>Phone</span> <a href="mailto:x.y@z.io?subject=hi">write</a><script>q@r.com</script></p>'
    assert page_emails(html_text(html)) == ["a@b.com", "x.y@z.io"]
