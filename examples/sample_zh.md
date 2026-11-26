# 示例：中文学术文档的 Markdown 样本

这份 Markdown 用于演示 Godfather 的 Markdown→LaTeX 流程：结构解析、块切分、转换、编译与批注。

## 1 引言

在低预算条件下训练专用小模型，关键在于把有限的人工注意力集中到最值得改进的样例上。
本文给出一个可复现的最小流程，先建立工具基线，再引入教师模型。

设训练样本数为 $N$，其中被人工复核的比例为 $p$，则人工成本近似为

$$
C_{human} = N \cdot p \cdot c_{label}
$$

其中 $c_{label}$ 是单条标注成本。

### 1.1 相关工作

- 响应级蒸馏：用教师输出的答案作为监督信号
- 偏好优化：同一输入下的 chosen/rejected 对
- 规则合成：由确定性算法产生标签

> 注意：评分、批注或对话本身不等于强化学习。

## 2 方法

本文采用如下流水线：

1. 解析 Markdown 结构树并分配稳定 `block_id`
2. 按结构切块，不在公式或表格中间截断
3. 组装完整 LaTeX 文档
4. 在隔离环境中编译并记录日志

### 2.1 数据切分原则

| 集合 | 用途 | 能否进入提示词示例 |
| --- | --- | --- |
| train | 训练 | 是 |
| dev | 迭代 | 是 |
| holdout | 最终验收 | 否 |

### 2.2 参数

```python
def split_by_group(group_id: str, seed: str = "godfather-1") -> str:
    """同一分组永远落在同一个集合。"""
    digest = hashlib.sha256(f"{seed}::{group_id}".encode("utf-8")).hexdigest()
    return "train" if int(digest[:8], 16) % 10 < 8 else "dev"
```

## 3 实验设置

我们使用 QLoRA 4-bit、序列长度 2048、microbatch 1、梯度累积 16 作为候选配置。

![示意图](figures/overview.png)

## 4 结论

在缺少付费教师 API 的情况下，本地模型 + 人工导入仍然可以支撑完整的训练闭环。
