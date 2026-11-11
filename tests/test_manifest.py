# -*- coding: utf-8 -*-
"""清单与分发边界静态检查（规范 §2、§8、§11.2、§19.2）。

只读文本与文件元数据，不联网、不导入插件、不执行插件代码。
严格模式（发布前）：设置环境变量 JINGJING_STRICT_PUBLISH=1，
此时 author 仍是占位名会让测试失败。
"""

import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PLACEHOLDER_AUTHORS = {
    "jingjingtown",
    "your name",
    "yourname",
    "author",
    "telyin",
    "anonymous",
    "todo",
    "tyour name",
}

WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
}

SCHEMA_TYPES = {"string", "integer", "number", "boolean", "array", "object"}
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
SAFE_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


def read_text(relative):
    with open(os.path.join(ROOT, relative), "r", encoding="utf-8") as handle:
        return handle.read()


def read_json(relative):
    return json.loads(read_text(relative))


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.pkg = read_json("package.json")

    def test_name_is_single_safe_segment(self):
        name = self.pkg["name"]
        self.assertIsInstance(name, str)
        self.assertTrue(SAFE_NAME.match(name), "name 必须是单段小写英文+连字符：%r" % name)
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)
        self.assertNotIn("..", name)
        self.assertNotIn("@", name)
        self.assertEqual(name, name.strip())
        self.assertNotIn(name.lower(), WINDOWS_RESERVED)

    def test_version_is_semver(self):
        self.assertTrue(SEMVER.match(self.pkg["version"]), self.pkg["version"])

    def test_author_is_present_and_not_anonymous(self):
        author = self.pkg.get("author")
        self.assertIsInstance(author, str)
        self.assertTrue(author.strip(), "author 不能为空")
        if os.environ.get("JINGJING_STRICT_PUBLISH") == "1":
            self.assertNotIn(
                author.strip().lower(),
                PLACEHOLDER_AUTHORS,
                "发布前必须把 author 改成真实维护者名称（规范 §2）",
            )
        else:
            if author.strip().lower() in PLACEHOLDER_AUTHORS:
                print("\n[提示] author 仍是占位名 %r，发布前必须替换。" % author)

    def test_main_entry_is_inside_package(self):
        main = self.pkg["main"]
        self.assertEqual(main, os.path.basename(main), "main 必须指向包内文件，不得越界")
        self.assertTrue(os.path.isfile(os.path.join(ROOT, main)))

    def test_description_is_full_and_complete(self):
        description = self.pkg["description"]
        self.assertIsInstance(description, str)
        self.assertGreater(len(description), 60, "描述过短，市场列表两行展示会显得空洞")
        self.assertIn("鲸鲸小镇", description)
        self.assertTrue(description.endswith("。"), "描述应在完整句末结束，不要截断")

    def test_keywords_declare_market(self):
        keywords = self.pkg["keywords"]
        self.assertIn("core-infinity", keywords)

    def test_license_declared(self):
        self.assertTrue(self.pkg["license"])
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "LICENSE")))

    def test_infinity_block(self):
        block = self.pkg["telyincore"]
        self.assertEqual(block["kind"], "infinity")
        self.assertIs(type(block["api"]), int)
        self.assertEqual(block["api"], 1)
        self.assertTrue(block["displayName"])
        self.assertIn(block["icon"], {"chat", "eye", "book", "contacts", "chip", "history", "grid", "store", "puzzle"})

    def test_config_files_are_inside_package_and_exist(self):
        block = self.pkg["telyincore"]
        for key in ("configSchema", "configPage"):
            value = block[key]
            self.assertEqual(value, os.path.basename(value), "%s 不得越界" % key)
            self.assertTrue(os.path.isfile(os.path.join(ROOT, value)), value)


class ConfigPageTest(unittest.TestCase):
    def setUp(self):
        self.html = read_text("config.html")

    def test_size_limit(self):
        size = os.path.getsize(os.path.join(ROOT, "config.html"))
        self.assertLessEqual(size, 256 * 1024, "配置页不得超过 256KB（规范 §8）")

    def test_single_file_no_external_resources(self):
        lowered = self.html.lower()
        self.assertNotIn("<script src=", lowered)
        self.assertNotIn("<link rel=\"stylesheet\" href=", lowered)
        self.assertNotIn("@import", lowered)

    def test_uses_config_bridge_only(self):
        self.assertIn("telyincore:config:get", self.html)
        self.assertIn("telyincore:config:save", self.html)
        self.assertIn("event.source !== parent", self.html)

    def test_no_network_from_page(self):
        lowered = self.html.lower()
        self.assertNotIn("fetch(", lowered)
        self.assertNotIn("xmlhttprequest", lowered)
        self.assertNotIn("navigator.sendbeacon", lowered)

    def test_pairing_code_field_is_plain_and_not_prefilled(self):
        # 接入码是用户从网页复制的一次性值，不是长期密钥；页面不应预填任何内容
        self.assertIn('id="pairing_code"', self.html)
        self.assertNotIn("pairing_code\" value=", self.html)
        self.assertNotIn("pairing_token", self.html)


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.schema = read_json("config.schema.json")
        self.example = read_json("config.example.json")

    def test_schema_is_lightweight_and_declares_defaults(self):
        properties = self.schema["properties"]
        self.assertIsInstance(properties, dict)
        for name, spec in properties.items():
            self.assertIn(spec["type"], SCHEMA_TYPES, name)
            self.assertIn("title", spec, name)
            self.assertIn("description", spec, name)
            if spec["type"] in ("integer", "number"):
                self.assertIn("minimum", spec, name)
                self.assertIn("maximum", spec, name)

    def test_example_matches_schema_and_has_no_secret(self):
        self.assertEqual(set(self.example), set(self.schema["properties"]))
        self.assertEqual(self.example["pairing_code"], "", "示例配置不得包含可用接入码")
        self.assertNotIn("@", self.example["service_url"], "示例地址不得内嵌账号")

    def test_no_long_lived_token_in_config_schema(self):
        # 长期令牌由插件自己在 dataDir/role.json 里保管，配置里只留一次性接入码
        properties = self.schema["properties"]
        self.assertIn("pairing_code", properties)
        self.assertNotIn("pairing_token", properties)


class DistributionBoundaryTest(unittest.TestCase):
    def test_private_config_not_shipped(self):
        for name in ("config.json", ".env"):
            self.assertFalse(
                os.path.isfile(os.path.join(ROOT, name)),
                "%s 不得随插件分发" % name,
            )

    def test_gitignore_covers_secrets(self):
        ignored = read_text(".gitignore")
        for entry in ("config.json", ".config-backups/", ".env"):
            self.assertIn(entry, ignored)

    def test_archive_size_under_limit(self):
        total = 0
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
            for name in files:
                total += os.path.getsize(os.path.join(base, name))
        self.assertLess(total, 64 * 1024 * 1024, "归档总量不得超过 64 MiB（规范 §11.2）")

    def test_required_delivery_docs_exist(self):
        for name in ("README.md", "CHANGELOG.md", "LICENSE", "docs/publishing.md"):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, name)), name)

    def test_readme_states_not_run_boundary(self):
        readme = read_text("README.md")
        self.assertIn("NOT_RUN", readme)
        self.assertIn("64 MiB", readme)


if __name__ == "__main__":
    unittest.main(verbosity=2)
