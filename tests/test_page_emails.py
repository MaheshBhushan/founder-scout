from founder_scout.scout import page_emails


def test_strips_button_text_glued_to_address():
    text = "eleanor.wicks@lifeyear.comCopy · t.m@hopper-mobility.comWeitere"
    assert page_emails(text) == ["eleanor.wicks@lifeyear.com", "t.m@hopper-mobility.com"]


def test_keeps_real_long_tlds():
    text = "a@b.design c@d.network e@f.company"
    assert page_emails(text) == ["a@b.design", "c@d.network", "e@f.company"]
