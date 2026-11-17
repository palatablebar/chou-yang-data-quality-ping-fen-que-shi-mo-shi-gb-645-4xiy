"""工单配对：按业务键（工单号）把两批采纳行分成 共有 / 仅旧批 / 仅新批。

配对只用工单号单元格的归一取值（M2 已 NFKC + strip），缺失或格式不合格
的工单号不参与配对，单独计入 unkeyed，保证任何一行都有明确去向，
且「共有 + 仅本批 + 无键 = 采纳行」逐行守恒。
"""
from __future__ import annotations

KEY_COLUMN = "ticket_id"


def _row_key(row: dict) -> str | None:
    cell = row["cells"][KEY_COLUMN]
    if cell["state"] != "value":
        return None
    value = cell.get("value")
    if value in (None, ""):
        return None
    return str(value)


def bucket(profile: dict) -> dict:
    """返回 行下标 → 配对键 的映射与三种桶（下标列表，保持源文件行序）。"""
    rows = profile["rows"]
    keys: list[str | None] = []
    keyed: dict[str, list[int]] = {}
    unkeyed: list[int] = []
    for index, row in enumerate(rows):
        key = _row_key(row)
        keys.append(key)
        if key is None:
            unkeyed.append(index)
        else:
            keyed.setdefault(key, []).append(index)
    return {"rows": rows, "keys": keys, "keyed": keyed, "unkeyed": unkeyed}


def partition(base_profile: dict, target_profile: dict) -> dict:
    """切出六组行下标：base_common / base_only / target_common / target_only
    以及两批各自的无键行。共有键的全部行（含同键重复）都进共有桶。
    """
    base = bucket(base_profile)
    target = bucket(target_profile)
    common_keys = base["keyed"].keys() & target["keyed"].keys()
    base_common: list[int] = []
    target_common: list[int] = []
    base_only: list[int] = []
    target_only: list[int] = []
    for key, indexes in base["keyed"].items():
        (base_common if key in common_keys else base_only).extend(indexes)
    for key, indexes in target["keyed"].items():
        (target_common if key in common_keys else target_only).extend(indexes)
    base_common.sort()
    target_common.sort()
    base_only.sort()
    target_only.sort()
    return {
        "common_keys": sorted(common_keys),
        "base_common": base_common,
        "base_only": base_only,
        "base_unkeyed": list(base["unkeyed"]),
        "target_common": target_common,
        "target_only": target_only,
        "target_unkeyed": list(target["unkeyed"]),
    }
