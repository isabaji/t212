from datetime import timedelta

from conftest import NOW, rss
from t212bot import companies, news


def test_name_matching_is_whole_word_and_case_insensitive():
    assert companies.is_relevant("AAPL", "Apple customers say their folding iPhones are cracking")
    assert companies.is_relevant("AAPL", "APPLE'S new iPad Mini")
    assert companies.is_relevant("AAPL", "Peter Thiel's Apple verdict raises a $5 trillion question")
    assert not companies.is_relevant("AAPL", "What Few Investors Realize About QQQ's Dividend")
    assert not companies.is_relevant("AAPL", "What Should Netflix Stock Investors Be Watching Now?")
    assert not companies.is_relevant("AAPL", "Pineapple exports surge")          # not a whole word


def test_short_tickers_never_match_as_english_words():
    assert not companies.is_relevant("CAT", "The cat is out of the bag for retail stocks")
    assert companies.is_relevant("CAT", "Caterpillar beats estimates")
    assert companies.is_relevant("CAT", "Dow movers: CAT, BA lead industrials higher")  # upper-case token
    assert not companies.is_relevant("V", "V-shaped recovery for small caps")
    assert companies.is_relevant("V", "Visa raises outlook")
    assert not companies.is_relevant("MA", "Ma Bell's successor reports earnings")
    assert companies.is_relevant("KO", "Coca-Cola tops expectations")
    assert not companies.is_relevant("KO", "Boxer wins by KO in round two")        # 2 letters: names only


def test_unknown_tickers():
    assert companies.is_relevant("AMD", "AMD unveils new chips")                    # ticker token
    assert not companies.is_relevant("AMD", "Chipmakers rally on AI demand")
    assert companies.is_relevant("ZZ", "Anything at all")                           # can't judge -> keep


def test_extra_names_from_env():
    extra = companies.parse_extra("AMD=Advanced Micro Devices|AMD; TSM = TSMC | Taiwan Semiconductor ;junk")
    assert extra == {"AMD": ["Advanced Micro Devices", "AMD"], "TSM": ["TSMC", "Taiwan Semiconductor"]}
    names = companies.names_for("AAPL=Cupertino giant;AMD=Advanced Micro Devices")
    assert names["AAPL"][0] == "Cupertino giant" and "Apple" in names["AAPL"]
    assert companies.is_relevant("AMD", "Advanced Micro Devices lifts guidance", names)
    assert companies.COMPANY_NAMES["AAPL"][0] == "Apple"                            # defaults untouched


class FakeResp:
    def __init__(self, text):
        self.text, self.ok, self.status_code = text, True, 200


class FakeSession:
    def __init__(self):
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        return FakeResp(rss([("Apple raises guidance, beats estimates", NOW - timedelta(hours=1)),
                             ("Netflix stock jumps on subscriber beat", NOW - timedelta(hours=2)),
                             ("Qualcomm just dropped 13% in a month", NOW - timedelta(hours=3))]))


def test_fetch_headlines_applies_relevance_filter(caplog):
    sess = FakeSession()
    with caplog.at_level("INFO"):
        heads = news.fetch_headlines(["AAPL"], now=NOW, session=sess)
    assert [h.title for h in heads["AAPL"]] == ["Apple raises guidance, beats estimates"]
    assert "kept 1 of 3 headlines" in caplog.text
    heads = news.fetch_headlines(["AAPL"], now=NOW, session=FakeSession(), require_relevance=False)
    assert len(heads["AAPL"]) == 3


def test_google_fallback_queries_by_company_name():
    class YahooDown(FakeSession):
        def get(self, url, **kw):
            self.urls.append(url)
            if "yahoo" in url:
                raise news.requests.ConnectionError("down")
            return super().get(url, **kw)
    sess = YahooDown()
    news.fetch_headlines(["AAPL"], now=NOW, session=sess)
    assert any("news.google.com" in u and "q=Apple+stock" in u for u in sess.urls), sess.urls
