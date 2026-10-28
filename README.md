# Typesense 检索核心优化

项目已经开始实施。原始方案保留在 [核心优化实施方案](Typesense核心优化实施方案.md)，当前代码和验证入口见 [retrieval-study](retrieval-study/README.md)。

- `typesense/`：上游源码子模块，固定在 `5dbf03ca8d44845746562cf59487698eb73161b9`，保留上游历史和许可证。
- `retrieval-study/`：自己编写的复现、观测、差分工具、核心补丁和原始证据。
- [首轮实施记录](retrieval-study/reports/2026-10-26-first-results.md)：实测事实、补丁边界和未完成项。

已在受控数据上复现强选择性过滤下的前缀遍历增长，并实现了内部节点遍历期间的截止检查补丁。原有 44 项 ART 测试通过；新增两项回归在原版失败、修改版通过；216 组核心差分无结果差异。

目前交付的是首轮核心实现与验证。完整服务器基线的最新构建因 `elfutils` 依赖编译错误失败；补丁保存在独立目录，未混入原版源码。尚未完成服务级差分、全部回归和正式性能验收，未向上游发布 issue 或 PR。

2026-10-28 已恢复中断的构建，并补充服务更新 / 删除 / 重插等 109 组场景的同版本重复核对。构建状态与错误摘要见 [构建快照](retrieval-study/raw/server-build-snapshot-20261028.json)；本机完整日志为 `retrieval-study/raw/v31-baseline-cached-20261028-build.log`。依赖下载使用保持上游版本和哈希不变的本地缓存。

## 获取项目

```sh
git clone --recurse-submodules https://github.com/ContinueForever/typesense.git
cd typesense/retrieval-study
python3 -m unittest discover -s tests -v
```

若已克隆但没有源码，运行 `git submodule update --init --recursive`。

仓库提交研究代码、方案、补丁、测试和合成数据验证证据。工具链、构建缓存、服务器数据目录与可重新生成的大批数据保留在本机。公开证据的范围见 [记录说明](retrieval-study/raw/README.md)，发布时的文件校验和见 `publication-manifest.json`。完整服务器的构建状态是发布时的快照，不能视为补丁已通过服务级验收。

