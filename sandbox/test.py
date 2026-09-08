"""使用快速排序（Quick Sort）对可迭代对象进行排序。

快速排序算法复杂度：
    平均时间复杂度：O(n log n)
    最坏时间复杂度：O(n²)
    空间复杂度：平均 O(log n)（用于保存待处理区间）

实现思路：
    1. 选择区间中间元素的键作为基准值（pivot）。
    2. 使用三路分区，将元素划分为排在基准值之前、等于基准值、
       排在基准值之后的三个区域。
    3. 继续排序基准值两侧的区域，直到所有区间都有序。

特性：
    - 不稳定排序：相等元素的相对顺序可能改变。
    - 在输入副本上原地排序，不会修改传入的可迭代对象。
    - 支持与 :func:`sorted` 类似的 ``key`` 和 ``reverse`` 参数。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, MutableSequence
from typing import Any


def _comes_before(a: Any, b: Any, reverse: bool) -> bool:
    """判断键 ``a`` 是否应排在键 ``b`` 之前。"""
    return b < a if reverse else a < b


def _partition(
    arr: MutableSequence[Any],
    keys: MutableSequence[Any],
    low: int,
    high: int,
    reverse: bool,
) -> tuple[int, int]:
    """对闭区间 ``[low, high]`` 三路分区，返回等值区域的边界。"""
    pivot = keys[(low + high) // 2]
    before = low
    current = low
    after = high

    while current <= after:
        if _comes_before(keys[current], pivot, reverse):
            arr[before], arr[current] = arr[current], arr[before]
            keys[before], keys[current] = keys[current], keys[before]
            before += 1
            current += 1
        elif _comes_before(pivot, keys[current], reverse):
            arr[current], arr[after] = arr[after], arr[current]
            keys[current], keys[after] = keys[after], keys[current]
            after -= 1
        else:
            current += 1

    return before, after


def _quick_sort(
    arr: MutableSequence[Any],
    keys: MutableSequence[Any],
    reverse: bool,
) -> None:
    """使用显式栈对 ``arr`` 原地执行快速排序。"""
    pending = [(0, len(arr) - 1)]

    while pending:
        low, high = pending.pop()

        # 继续处理较小的一侧，把较大的一侧放入栈中，以限制栈深度。
        while low < high:
            equal_low, equal_high = _partition(arr, keys, low, high, reverse)
            left = (low, equal_low - 1)
            right = (equal_high + 1, high)
            left_size = left[1] - left[0] + 1
            right_size = right[1] - right[0] + 1

            if left_size < right_size:
                if right[0] < right[1]:
                    pending.append(right)
                low, high = left
            else:
                if left[0] < left[1]:
                    pending.append(left)
                low, high = right


def quick_sort(
    iterable: Iterable[Any],
    *,
    key: Callable[[Any], Any] | None = None,
    reverse: bool = False,
) -> list[Any]:
    """返回使用快速排序得到的新列表。

    :param iterable: 待排序的可迭代对象。
    :param key: 与 :func:`sorted` 相同的键函数；为 ``None`` 时直接比较元素本身。
    :param reverse: ``True`` 时按降序排列。
    :return: 排序后的新列表。
    """
    arr = list(iterable)
    if len(arr) < 2:
        # 长度小于 2 的数组天然有序，无需排序。
        return arr

    effective_key: Callable[[Any], Any] = key if key is not None else lambda item: item
    keys = [effective_key(item) for item in arr]
    _quick_sort(arr, keys, reverse)
    return arr


if __name__ == "__main__":
    demo = [5, 3, 8, 1, 9, 2, 7, 4, 6]
    print("原始数组:", demo)
    print("升序排列:", quick_sort(demo))

    words = ["banana", "apple", "cherry", "date"]
    print("按长度排序:", quick_sort(words, key=len))

    nums = [5, 3, 8, 1, 9, 2, 7, 4, 6]
    print("降序排列:", quick_sort(nums, reverse=True))

    # 边界情况：空列表 / 单元素 / 已排序 / 含重复元素。
    print("空列表:", quick_sort([]))
    print("单元素:", quick_sort([42]))
    print("含重复:", quick_sort([3, 1, 2, 1, 3]))
