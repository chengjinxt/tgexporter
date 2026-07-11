from tgexporter.filename import article_filename, media_filename, sanitize_title


def test_sanitize_title_replaces_windows_unsafe_characters():
    assert sanitize_title('"A/B:C*?<>|') == "“A、B：C？《》｜"


def test_article_and_media_filenames_share_prefix():
    title = "OpenClaw 原生移动端上线 iOS 与 Android"
    assert article_filename("20260701", 1, title) == "20260701_001_OpenClaw 原生移动端上线 iOS 与 Android.md"
    assert (
        media_filename("20260701", 1, "PIC", 2, title, ".jpg")
        == "20260701_001_PIC_002_OpenClaw 原生移动端上线 iOS 与 Android.jpg"
    )


def test_sanitize_title_falls_back_for_corrupt_question_marks():
    assert sanitize_title("？？？？？？？？ ？？ “？？“12 ？？？？") == "未命名文章"
