# Typesense 检索核心优化

项目已经开始实施。原始方案保留在 [核心优化实施方案](Typesense核心优化实施方案.md)，当前代码和验证入口见 [retrieval-study](retrieval-study/README.md)。

- `typesense/`：上游源码子模块，固定在 `5dbf03ca8d44845746562cf59487698eb73161b9`，保留上游历史和许可证。
- `retrieval-study/`：自己编写的复现、观测、差分工具、核心补丁和原始证据。
- [最新完整服务验证](retrieval-study/reports/2026-10-28-full-server-validation.md)：完整构建、真实过滤入口回归、HTTP 差分与截止诊断。
- [本机扩展验证](retrieval-study/reports/2026-10-29-local-regression.md)：生产阈值以上的惰性过滤、JOIN、三组随机更新、并发读写及断连恢复。

已在受控数据上复现强选择性过滤下的前缀遍历增长，并实现了内部节点遍历期间的截止检查补丁。完整原版与补丁版均已从源码构建成功。用真实生产对象链接的 47 项聚焦回归中，原版通过 44 项既有测试、在 3 项新增截止回归上失败；补丁版 47/47 通过。

同一 v31 提交的 109 次服务状态场景、234 次查询矩阵均无普通结果差异；每版分别通过 24 项服务与 9 项小数据独立结果核对。删除高分文档后的 55 对截止诊断也通过，其中 15 对普通结果 / 恢复控制一致，40 对有限预算响应按显式截止契约核对。

本机 `elfutils` 兼容错误已通过有来源哈希的只读类型修正解决，两份服务使用相同依赖和构建参数。构建状态见 [当前快照](retrieval-study/raw/server-build-snapshot-current-20261028.json)，完整结果见 [验证摘要](retrieval-study/manifests/full-server-validation-20261028.json)。先前失败记录仍保留。

扩展验证新增 2,166 对请求、每版 173 项受限独立 / JOIN 检查，以及每版 1,232 项并发操作和恢复检查；56 条上游自然文本示例的 234 对请求也无差异。首版密集惰性过滤的 9 个普通控制在两版中均耗尽预算，负结果单独保留。

全套上游 C++ 测试正在推进，正式交替性能验收和上游评审仍待完成。此前短截止诊断每个用例每版只有 5 个样本，不能据此报告稳定加速比例或 P99。未向上游发布 issue 或 PR。

## 获取项目

```sh
git clone --recurse-submodules https://github.com/ContinueForever/typesense.git
cd typesense/retrieval-study
python3 -m unittest discover -s tests -v
```

若已克隆但没有源码，运行 `git submodule update --init --recursive`。

仓库提交研究代码、方案、补丁、测试和合成数据验证证据。工具链、构建缓存、服务器数据目录与可重新生成的大批数据保留在本机。公开证据的范围见 [记录说明](retrieval-study/raw/README.md)，发布时的文件校验和见 `publication-manifest.json`。完整服务器的构建状态是发布时的快照，不能视为补丁已通过服务级验收。

