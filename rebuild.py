import datetime
import errno
import json
import logging
import sys
import urllib.request
from collections import defaultdict
from os import makedirs as _makedirs  # noqa
from os.path import dirname, exists, join, realpath
from tempfile import NamedTemporaryFile
from urllib.parse import urljoin

from dotenv import dotenv_values
from feedgen.feed import FeedGenerator
from htmlmin import minify
from jinja2 import Environment, FileSystemLoader, select_autoescape
from openpyxl import load_workbook
from rcssmin import cssmin
from selenium import webdriver
from slugify import slugify

LINK_COLUMNS = (
    "title",
    "url",
    "desc",
    "category_str",
    "kind",
    "lang",
    "sender",
    "source",
    "create_time",
)
CATEGORY_COLUMN_INDEX = LINK_COLUMNS.index("category_str")
ENV = dotenv_values(join(dirname(realpath(__file__)), ".env"))

MINIFY_ARGS = {
    "remove_optional_attribute_quotes": False,
    "remove_comments": True
}

def minify(text, **args):
    return text


logger = logging.getLogger()
logger.setLevel(logging.INFO)

handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter(
    "%(asctime)s %(levelname)s - %(message)s"
)
handler.setFormatter(formatter)
logger.addHandler(handler)


def get_lines(worksheet):
    """Load lines from worksheet and return as list of lists.

    :param worksheet: Worksheet Object
    :return: list
    """
    logger.debug("Parsing lines from worksheet.")
    result = []
    for idx, row in enumerate(worksheet.rows):
        if idx == 0:
            continue
        result.append(list(map(lambda cell: cell.value, row)))
    return result


def get_category_parts(category_str):
    """
    Separate category to list items.

    :param category_str: String representing a category. E.g. "a > b > c
    :return: list of elements. E.g.: ["a", "b", "c]
    """
    separator = ENV["SPREADSHEET_CATEGORY_SEPARATOR"]
    return list(
        filter(
            lambda part: bool(part),
            [part.strip() for part in category_str.split(separator)],
        )
    )


def get_category_path(category_str):
    """
    Convert category string to a path.

    :param category_str: String representing a category. E.g. "a > b > c"
    :return: path string. /a/b/c/
    """
    parts = get_category_parts(category_str)
    return ("/".join(map(slugify, parts))) + "/"


def get_category_root_path(category_str):
    """
    Get relative root path for category.

    :param category_str: String representing a category. E.g. "a > b > c"
    :return: "../../../"
    """
    return "../" * (get_category_depth(category_str) + 1)


def get_category_depth(category_str):
    """
    Get depth of a category.

    :param category_str: String representing a category. E.g. "a > b > c"
    :return: 2
    """
    return category_str.count(ENV["SPREADSHEET_CATEGORY_SEPARATOR"])


def get_parent_category_str(category_str):
    """
    Get parent category str of category_str.

    :param category_str: String representing a category. E.g. "a > b > c"
    :return: "a > b"
    """
    if category_str is None:
        return None
    parts = get_category_parts(category_str)
    return (
        f" {ENV['SPREADSHEET_CATEGORY_SEPARATOR']} ".join(parts[:-1])
        if len(parts) > 1
        else None
    )


def get_link_from_row(link_row):
    """
    Get link information from a line in worksheet.

    :param link_row: List of items that represents a row in links page.
    :return: dict
    """
    link = {
        column: link_row[index] for index, column in enumerate(LINK_COLUMNS)
    }
    link["create_time"] = link["create_time"].replace(
        tzinfo=datetime.timezone(
            datetime.timedelta(hours=int(ENV.get("TIMEZONE_HOURS", "3")))
        )
    )
    link["file_path"] = (
        get_category_path(link_row[CATEGORY_COLUMN_INDEX])
        + slugify(link["url"])
        + ".html"
    )
    return link


def get_links_by_category(link_rows):
    logger.debug("Building links by category.")
    result = defaultdict(list)
    for link_row in link_rows:
        category_str = link_row[CATEGORY_COLUMN_INDEX]
        link = get_link_from_row(link_row)
        result[category_str].append(link)
    return result


def create_category_paths(base_path, link_rows):
    """
    Create folders of categories

    :param base_path: Base path for categories. E.g. "/tmp/foo/"
    :param link_rows: List of lists that represents a rows in links page.
    :return: None
    """
    logger.debug("Creating category paths.")
    category_str_list = get_links_by_category(link_rows).keys()
    for category_str in category_str_list:
        path = join(base_path, get_category_path(category_str))
        make_dirs(path)


def get_category_overrides(categories_page_rows):
    logger.debug("Getting category overrides.")
    overrides = {}
    for category_page_row in categories_page_rows:
        override = {}
        if len(category_page_row) > 1 and category_page_row[1] is not None:
            override["title"] = category_page_row[1]
        if len(category_page_row) > 2 and category_page_row[2] is not None:
            override["desc"] = category_page_row[2]
        overrides[category_page_row[0]] = override
    return overrides


def get_category_info(category_str, overrides):
    name = get_category_parts(category_str)[-1]
    result = {
        "name": name,
        "title": name,
        "desc": None,
        "parent": None,
        "path": get_category_path(category_str),
        "children": [],
    }
    result.update(overrides.get(category_str, {}))
    return result


def get_categories(links_page_rows, categories_page_rows):
    logger.info("Building category information.")
    categories = {}
    overrides = get_category_overrides(categories_page_rows)
    for row in links_page_rows:
        category_str = row[CATEGORY_COLUMN_INDEX]
        if category_str in categories:
            continue
        category = get_category_info(category_str, overrides)
        categories[category_str] = category

    for row in links_page_rows:

        child_category_str = row[CATEGORY_COLUMN_INDEX]
        parent_category_str = get_parent_category_str(child_category_str)

        while child_category_str:

            if child_category_str not in categories:
                categories[child_category_str] = get_category_info(
                    child_category_str, overrides
                )

            if parent_category_str and parent_category_str not in categories:
                categories[parent_category_str] = get_category_info(
                    parent_category_str, overrides
                )

            if child_category_str and child_category_str not in categories:
                categories[child_category_str] = get_category_info(
                    child_category_str, overrides
                )

            if parent_category_str and child_category_str:
                if categories[child_category_str]["parent"] is None:
                    categories[child_category_str][
                        "parent"
                    ] = parent_category_str
                if (
                    child_category_str
                    not in categories[parent_category_str]["children"]
                ):
                    categories[parent_category_str]["children"].append(
                        child_category_str
                    )

            child_category_str = parent_category_str
            parent_category_str = get_parent_category_str(child_category_str)

    return categories


def get_links_by_date(link_rows, reverse=True):
    links = []
    for row in link_rows:
        links.append(get_link_from_row(row))
    return sorted(links, key=lambda i: i["create_time"], reverse=reverse)


def render_sitemap(root_path, categories, links_by_category, sitemap_template):
    logger.info("Rendering sitemap.")
    with open(join(root_path, "sitemap.xml"), "w") as file:
        file.write(
            minify(
                sitemap_template.render(
                    root_path=root_path,
                    links_by_category=links_by_category,
                    categories=categories,
                    render_date=datetime.date.today(),
                    strftime=datetime.date.strftime,
                ), **MINIFY_ARGS
            )
        )


def render_feed(root_path, link_page_rows):
    logger.info("Rendering feed outputs.")
    feed = FeedGenerator()
    feed.id(ENV["SITE_URL"])
    feed.title(ENV["SITE_TITLE"])
    feed.link(href=ENV["SITE_URL"], rel="alternate")
    feed.subtitle(ENV["SITE_DESC"])
    feed.link(href=urljoin(ENV["SITE_URL"], "feed.rss"), rel="self")
    feed.language("tr")

    links = get_links_by_date(link_page_rows)
    for link in links:
        entry = feed.add_entry()
        entry.id(link["file_path"])
        entry.title(link["title"])
        entry.description(link["desc"])
        entry.link(
            title=link["title"],
            rel="alternate",
            type="text/html",
            href=urljoin(ENV["SITE_URL"], link["file_path"]),
        )
        entry.updated(link["create_time"])
    feed.rss_file(join(root_path, "rss.xml"), pretty=True)
    feed.atom_file(join(root_path, "atom.xml"), pretty=True)


def render_categories(base_path, links_by_category, categories, template):
    logger.info("Rendering categories.")
    for category_str, links in links_by_category.items():
        category = categories[category_str]
        file_path = join(base_path, category["path"], "index.html")
        root_path = get_category_root_path(category_str)
        with open(file_path, "w") as file:
            file.write(
                minify(
                    template.render(
                        site_title=ENV["SITE_TITLE"],
                        links=links,
                        root_path=root_path,
                        category=category,
                        categories=categories,
                        env=ENV,
                    ), **MINIFY_ARGS
                )
            )
    for category_str, category in categories.items():
        if category_str in links_by_category:
            continue
        file_path = join(base_path, category["path"], "index.html")
        root_path = get_category_root_path(category_str)
        with open(file_path, "w") as file:
            file.write(
                minify(
                    template.render(
                        links=[],
                        root_path=root_path,
                        category=category,
                        categories=categories,
                        env=ENV,
                    ), **MINIFY_ARGS))

def render_links(base_path, links_by_category, template, force=False):
    logger.info("Rendering links.")
    cleaner_js = """
        document.getElementsByTagName('header')[0].style.background='none';
        document.getElementsByTagName('form')[0].remove();
        document.getElementById('page').style.margin=0;
        document.getElementById('link_detail').style.margin=0;
        text = document.getElementsByTagName('h1')[0].textContent;
        document.getElementsByTagName('h1')[0].textContent = text.toUpperCase();
        document.getElementsByTagName('header')[0].style.background='none';
        document.getElementsByTagName('script')[0].remove();
        document.getElementsByClassName('meta')[0].remove();
        document.getElementsByClassName('socializer')[0].remove()
        document.getElementsByTagName('p')[1].classList.remove('mb');
    """
    browser = None
    for category_str, links in links_by_category.items():
        for link in links:
            file_path = join(base_path, link["file_path"])
            image_url = link["file_path"] + ".png"
            with open(file_path, "w") as file:
                file.write(
                    minify(
                        template.render(
                            link=link,
                            root_path=get_category_root_path(category_str),
                            image_url=image_url,
                            env=ENV,
                        ), **MINIFY_ARGS)
                )
            image_path = join(base_path, image_url)
            if force or not exists(image_path):
                if not browser:
                    browser = webdriver.Safari()
                    browser.set_window_size(600, 400)
                browser.get("file://" + join(base_path, file_path))
                browser.execute_script(cleaner_js)
                browser.save_screenshot(join(base_path, image_url))


def render_home(base_path, link_page_rows, categories, template):
    logger.info("Rendering homepage.")
    links = get_links_by_date(link_page_rows)
    last_update = datetime.date.today()
    file_path = join(base_path, "index.html")
    with open(file_path, "w") as file:
        file.write(
            minify(
                template.render(
                    latest_links=links[:50],
                    root_path="./",
                    categories=categories,
                    last_update=last_update,
                    num_of_links=len(link_page_rows),
                    env=ENV,
                ),
                **MINIFY_ARGS
            )
        )


def make_dirs(path):
    if exists(path):
        return
    try:
        _makedirs(path)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise


def build_assets(root_path):
    logger.info("Building assets.")
    with open(
        join("assets", "themes", ENV.get("THEME_FILE", "Retro Dark.css")), "r"
    ) as file:
        style = cssmin(file.read())
    with open(join(root_path, "style.css"), "w") as file:
        file.write(style)


def render_json(root_path, categories, links_by_category):
    logger.info("Building json output.")

    class DateTimeEncoder(json.JSONEncoder):
        def default(self, o):
            if isinstance(o, datetime.datetime):
                return o.isoformat()
            return json.JSONEncoder.default(self, o)

    with open(join(root_path, "data.json"), "w", encoding="utf8") as file:
        data = {
            "categories": categories,
            "links_by_category": links_by_category,
        }
        json.dump(data, file, cls=DateTimeEncoder, ensure_ascii=False)


def build(root_path=join(dirname(realpath(__file__)), "docs/")):
    jinja = Environment(
        loader=FileSystemLoader("templates/"),
        autoescape=select_autoescape(["html", "xml"]),
    )

    with NamedTemporaryFile(suffix=".xlsx") as spreadsheet_file:
        with urllib.request.urlopen(ENV["SPREADSHEET_URL"]) as remote_file:
            spreadsheet_file.write(remote_file.read())
            workbook = load_workbook(
                filename=spreadsheet_file.name, read_only=True
            )

    links_page_lines = get_lines(
        workbook[ENV.get("SPREADSHEET_LINKS_PAGE_NAME", "Links")]
    )

    categories_page_lines = get_lines(
        workbook[ENV.get("SPREADSHEET_CATEGORIES_PAGE_NAME", "Categories")]
    )

    category_template = jinja.get_template("category.html.jinja2")
    link_template = jinja.get_template("link.html.jinja2")
    home_template = jinja.get_template("home.html.jinja2")
    sitemap_template = jinja.get_template("sitemap.xml.jinja2")

    links_by_category = get_links_by_category(links_page_lines)
    categories = get_categories(links_page_lines, categories_page_lines)

    create_category_paths(root_path, links_page_lines)

    render_json(root_path, categories, links_by_category)
    build_assets(root_path)
    render_categories(
        root_path, links_by_category, categories, category_template
    )
    render_links(root_path, links_by_category, link_template)
    render_home(root_path, links_page_lines, categories, home_template)
    render_sitemap(root_path, categories, links_by_category, sitemap_template)
    render_feed(root_path, links_page_lines)


if __name__ == "__main__":
    build()
