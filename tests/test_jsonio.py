from __future__ import annotations

import unittest

import helpers  # noqa: F401

from review_agent.jsonio import JsonExtractionError, extract_json_array, extract_json_object


class ExtractArrayTest(unittest.TestCase):
    def test_plain_array(self):
        self.assertEqual(extract_json_array('[{"a": 1}]'), [{"a": 1}])

    def test_empty_array(self):
        self.assertEqual(extract_json_array("[]"), [])

    def test_code_fence(self):
        self.assertEqual(extract_json_array('```json\n[{"a": 1}]\n```'), [{"a": 1}])

    def test_cli_chatter_around_array(self):
        text = 'thinking...\nreading files\n[{"file": "a.py"}]\ndone in 3s'
        self.assertEqual(extract_json_array(text), [{"file": "a.py"}])

    def test_wrapped_in_findings_key(self):
        self.assertEqual(extract_json_array('{"findings": [{"a": 1}]}'), [{"a": 1}])

    def test_brackets_inside_strings_do_not_confuse(self):
        text = '[{"claim": "list[0] 접근이 잘못됐다"}]'
        self.assertEqual(extract_json_array(text)[0]["claim"], "list[0] 접근이 잘못됐다")

    def test_last_valid_array_wins(self):
        text = '[{"a": 1}]\n이어서 다시 출력합니다\n[{"a": 2}]'
        self.assertEqual(extract_json_array(text), [{"a": 2}])

    def test_no_array_raises(self):
        with self.assertRaises(JsonExtractionError):
            extract_json_array("지적할 것이 없습니다")


class ExtractObjectTest(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(extract_json_object('{"verdict": "real"}'), {"verdict": "real"})

    def test_object_with_preamble(self):
        text = '확인했습니다.\n{"verdict": "not_real", "reason": "의도된 동작"}'
        self.assertEqual(extract_json_object(text)["verdict"], "not_real")

    def test_fenced_object(self):
        self.assertEqual(
            extract_json_object('```\n{"verdict": "real"}\n```')["verdict"], "real"
        )

    def test_no_object_raises(self):
        with self.assertRaises(JsonExtractionError):
            extract_json_object("real")


if __name__ == "__main__":
    unittest.main()
