"""CI 必须执行预期测试数量；跳过不能冒充 PASS。"""

import sys
import xml.etree.ElementTree as ET


def check(path: str, expected: int) -> None:
    cases = ET.parse(path).getroot().findall(".//testcase")
    assert len(cases) == expected, f"Expected {expected} tests, found {len(cases)}"
    assert all(not any(c.find(tag) is not None for tag in ("skipped", "failure", "error")) for c in cases)
    print(f"Verified {expected} executed tests, zero failures/errors/skips")


if __name__ == "__main__":
    check(sys.argv[1], int(sys.argv[2]))
