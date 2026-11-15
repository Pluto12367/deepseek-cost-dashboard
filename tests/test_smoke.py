"""冒烟测试:验证核心模块可导入、静态资源可用。

注意:最后一个 test_cfi_pipeline_demo 是故意失败的演示测试,
用于验证 CFI 投递链路。在工作台确认收到记录后删除它。
"""

import json
from pathlib import Path


def test_core_modules_import():
    """项目核心模块应能正常导入。"""
    import db
    import estimator
    import notify

    assert db is not None
    assert estimator is not None
    assert notify is not None


def test_prices_json_valid():
    """价格表应为合法 JSON 对象且非空。"""
    prices_path = Path(__file__).resolve().parent.parent / "prices.json"
    data = json.loads(prices_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert len(data) > 0


def test_cfi_pipeline_demo():
    """临时:故意失败,验证 CFI 投递链路。验证成功后删除本函数。"""
    assert False, "CFI 投递链路演示——在工作台看到记录后删除本测试"
