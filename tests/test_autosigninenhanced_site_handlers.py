import ast
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Tuple
from urllib.parse import urljoin, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SITES_DIR = PROJECT_ROOT / "plugins.v2" / "autosigninenhanced" / "sites"
LOGGER = SimpleNamespace(
    debug=lambda *args, **kwargs: None,
    error=lambda *args, **kwargs: None,
    info=lambda *args, **kwargs: None,
    warn=lambda *args, **kwargs: None,
)


class HandlerBase:
    @staticmethod
    def sign_in_result(html_res, regexs):
        return any(re.search(regex, html_res) for regex in regexs)


class StringUtils:
    @staticmethod
    def url_equal(url, expected):
        def domain(value):
            parsed = urlparse(value if "://" in value else f"https://{value}")
            return (parsed.hostname or "").removeprefix("www.")

        return domain(url) == domain(expected)


def load_handler(module_name, class_name, **dependencies):
    module_path = SITES_DIR / f"{module_name}.py"
    assert module_path.exists(), f"Missing site handler: {module_path.name}"

    module_ast = ast.parse(
        module_path.read_text(encoding="utf-8"),
        filename=str(module_path),
    )
    class_node = next(
        node
        for node in module_ast.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    namespace = {"__name__": f"test_{module_name}"}
    namespace.update(dependencies)
    exec(
        compile(ast.Module(body=[class_node], type_ignores=[]), str(module_path), "exec"),
        namespace,
    )
    return namespace[class_name]


def load_haidan_handler():
    return load_handler(
        "haidan",
        "HaiDan",
        CommentedMap=dict,
        StringUtils=StringUtils,
        Tuple=Tuple,
        _ISiteSigninHandler=HandlerBase,
        logger=LOGGER,
        urljoin=urljoin,
    )


def load_hdbao_handler(request_utils=object):
    return load_handler(
        "hdbao",
        "HDBao",
        CommentedMap=dict,
        RequestUtils=request_utils,
        StringUtils=StringUtils,
        Tuple=Tuple,
        _ISiteSigninHandler=HandlerBase,
        logger=LOGGER,
        settings=SimpleNamespace(PROXY="configured-proxy"),
        urljoin=urljoin,
    )


def test_haidan_matches_old_and_new_domains():
    handler = load_haidan_handler()

    assert handler.match("https://www.haidan.cc/")
    assert handler.match("https://haidan.video/index.php")
    assert not handler.match("https://example.com/")


def test_haidan_signin_uses_configured_domain():
    handler = load_haidan_handler()()
    requested_urls = []
    page_sources = iter(["", '<input value="已经打卡">'])

    def get_page_source(**kwargs):
        requested_urls.append(kwargs["url"])
        return next(page_sources)

    handler.get_page_source = get_page_source

    result = handler.signin(
        {
            "name": "HaiDan",
            "url": "https://www.haidan.cc/",
            "cookie": "cookie",
            "ua": "user-agent",
            "proxy": False,
            "render": True,
            "timeout": 30,
        }
    )

    assert result == (True, "签到成功")
    assert requested_urls == [
        "https://www.haidan.cc/signin.php",
        "https://www.haidan.cc/index.php",
    ]


def test_hdbao_matches_site_domain():
    handler = load_hdbao_handler()

    assert handler.match("https://hdbao.cc/attendance.php")
    assert not handler.match("https://example.com/")


def test_hdbao_signin_posts_attendance_form():
    request_options = []
    post_calls = []

    class RequestUtils:
        def __init__(self, **kwargs):
            request_options.append(kwargs)

        def post_res(self, **kwargs):
            post_calls.append(kwargs)
            return SimpleNamespace(
                status_code=200,
                text=(
                    "这是您的第 <b>1</b> 次签到，已连续签到 <b>1</b> 天，"
                    "本次签到获得 <b>10</b> 个魔力值。"
                ),
            )

    handler = load_hdbao_handler(RequestUtils)()

    result = handler.signin(
        {
            "name": "HDBao",
            "url": "https://hdbao.cc/",
            "cookie": "cookie",
            "ua": "user-agent",
            "proxy": True,
            "timeout": 30,
        }
    )

    assert result == (True, "签到成功")
    assert request_options == [
        {
            "cookies": "cookie",
            "ua": "user-agent",
            "proxies": "configured-proxy",
            "timeout": 30,
        }
    ]
    assert post_calls == [
        {
            "url": "https://hdbao.cc/attendance.php",
            "data": {},
        }
    ]


def test_hdbao_treats_existing_attendance_as_success():
    class RequestUtils:
        def __init__(self, **kwargs):
            pass

        def post_res(self, **kwargs):
            return SimpleNamespace(
                status_code=200,
                text="您今天已经签到过了，请勿重复刷新。",
            )

    handler = load_hdbao_handler(RequestUtils)()

    result = handler.signin(
        {
            "name": "HDBao",
            "url": "https://hdbao.cc/",
            "cookie": "cookie",
            "ua": "user-agent",
            "proxy": False,
            "timeout": 30,
        }
    )

    assert result == (True, "今日已签到")
