"""收集阶段的确定性失败类型。"""


class CollectionError(Exception):
    """资料包无法成立的任何情况（缺对象、声明不符、超预算、白名单等）。"""
