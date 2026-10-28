# Typesense 前缀检索研究

当前成果：确定性复现工具、真实 ART / 倒排源码探针、可审查的截止检查补丁、完整服务构建及首轮原版 / 补丁版服务验证。完整项目尚未验收。

## 当前进度

| 项目 | 状态与证据 |
| --- | --- |
| 版本固定与查重 | v31 `5dbf03ca8d44845746562cf59487698eb73161b9`；30.2 官方二进制独立记录；issue、PR、发行记录保存在 `manifests/` |
| 用户报告的关键现象 | 30.2 上复现非空强过滤的短前缀开销增长；没有复现空过滤也慢 |
| 核心机制 | 10 万文档探针中，`k=1` 仍访问 99,993 个叶子、拒绝 99,992 个，队列峰值 87,581 |
| 首个核心改动 | 两个 `art_topk_iter` 入口改为每 1,024 个队列工作单元检查截止，覆盖内部节点扩展 |
| 原有 ART 回归 | 原版与候选版各 44/44 通过 |
| 新回归 | 生产对象链接的原版 0/3、候选 3/3，已执行真实迭代器入口；结合既有 ART 为候选 47/47 |
| 核心差分 | 216 组查询，候选内容、顺序与未截止状态无差异，范围为物化 ID 过滤入口 |
| HTTP 工具核对 | 78 个用例 × 3 次的同版本重复运行无差异；受限独立 oracle 9/9 通过 |
| 内存检查 | ASan 通过；PIC 配置下原版 / 候选的 216 组 ASan + UBSan 差分通过。默认配置 TLS 报告仍保留，未宣称该配置通过 |
| 工具测试 | 当前 16 项 Python 单元测试通过，新增截止差分的完整性和部分结果契约检查 |
| 服务状态场景 | v31 原版 / 补丁版 109 对无差异，每版 24 项受限 oracle 通过 |
| 查询矩阵 | 同一数据 78 类 × 3 次，234 对无差异，每版 9 项受限 oracle 通过 |
| 完整服务 | 原版与补丁版均成功，构建参数与共同依赖一致，来源核验通过 |
| 截止 HTTP 诊断 | 98,304 条导入、删除 32,768 条高分文档；55 对检查通过，包含 15 对普通控制与 40 对有限预算响应 |
| 上游提交 | 未创建 issue、PR，未签署贡献者协议 |

主报告：[首轮实施结果](reports/2026-10-26-first-results.md)。链路和设计：[请求链路与补丁设计](reports/request-path-and-design.md)。

最新结果：[完整服务验证](reports/2026-10-28-full-server-validation.md)，机器可读摘要为 `manifests/full-server-validation-20261028.json`。原有恢复记录和失败快照保留其历史状态。

## 目录与贡献边界

```text
../typesense/       固定上游 C++ 引擎；原版二进制保存后，本地检出应用候选补丁
study.py            数据生成、环境记录、专用服务启动、导入校验、查询、oracle、HTTP 差分
bench/              核心构建、观测、对照和服务器构建工具
tests/              实验工具的完整性回归
patches/            最小核心补丁和对应 GoogleTest 源码
manifests/          上游审计、版本、工具链、依赖和环境记录
reports/            机制、设计、验证与未完成项
raw/                原始请求、导入结果、资源、测试和构建日志
builds/             二进制、构建缓存、候选源码投影（不纳入研究源码）
data/               可以按种子重建的数据（不纳入研究源码）
tools/              项目本地工具链（不安装系统软件）
```

上游已有 ART、倒排、过滤参与、空集退出、API、网络和复制能力。本阶段自己编写了实验工具、观测注入、回归测试和截止检查补丁。没有实现新的检索引擎，没有新增过滤下推，没有证明解决一般短前缀查询的全部性能问题。

## 本机运行

以下命令在 `/home/hermes/typesense/retrieval-study` 运行，研究脚本需要 Python 3.11 或更新版本。输出目录必须是新目录，工具拒绝覆盖旧实验。测试进程只监听回环地址，由启动它的工具负责退出；数据目录保留供复查。

```sh
python3 -m unittest discover -s tests -v

python3 study.py generate --output data/demo-256 --documents 256
python3 study.py run \
  --binary builds/release-30.2/typesense-server \
  --dataset data/demo-256 --output raw/demo-256 --repeats 3
python3 study.py oracle raw/demo-256
```

每次运行保存环境和二进制哈希，逐条核对导入结果、集合数量，并通过导出内容的顺序无关摘要确认数据一致。查询结果、错误、超时、截止与热身请求均保留。新版运行器另外保存逐轮 CPU、RSS、峰值 RSS 和换页信息。

小样本只报告样本中位数、范围与错误数量；成功样本不足 10,000 时不生成 P99。`compare` 拒绝缺失请求、重复请求、未完成运行和不一致的参数，不把错误或截止响应当成普通正确性样本。

## 重跑机制实验

```sh
python3 study.py generate --output data/demo-10k --documents 10000
python3 study.py generate --output data/demo-100k --documents 100000
python3 study.py generate --output data/demo-100k-u1k \
  --documents 100000 --other-unique 1000
python3 study.py generate --output data/demo-100k-v1k \
  --documents 100000 --hot-terms 1000

python3 study.py run \
  --binary builds/release-30.2/typesense-server \
  --dataset data/demo-100k --output raw/demo-100k \
  --repeats 5 --query-id p918-narrow-k1 --query-id p918-empty-k1 \
  --query-id p918-unfiltered-k1 --query-id exact-control
```

目标租户固定 8 条文档，其中 1 条匹配前缀。`--other-unique` 控制其他租户词项基数，`--hot-terms` 控制其中共享热门前缀的词项数。编号全部为合成数据。`--infix-index` 只控制索引开关，查询开关在生成的 `queries.json` 中显式为 `off`。

完整枚举 oracle 只用于生成器在小数据上标记的模式，运行时再次验证候选上限足够。存在候选截断的查询只做同版本差分。

## 重建核心与补丁

核心探针直接编译上游 `art.cpp`、posting、数组和 libfor 源码。链接器移除未使用的服务接口；没有替代 ART、倒排或过滤实现。探针目前不链接惰性过滤迭代器，不能代替完整服务器测试。

依赖头文件来自锁定的上游 Bazel 构建缓存；glog 0.6.0、GoogleTest 1.12.1 与 ART 测试数据使用上游版本。全部路径、编译命令与哈希保存在各构建的 `manifest.json` 和 `manifests/versions.json`。本机已完成这些依赖准备；在新机器上应先按上游 CI 准备工具链并抓取 WORKSPACE 依赖。

```sh
python3 bench/prepare_candidate.py --output builds/demo-candidate

python3 bench/build_core.py --variant demo-core-original \
  --ref 5dbf03ca8d44845746562cf59487698eb73161b9 --observe
python3 bench/build_core.py --variant demo-core-modified \
  --ref 5dbf03ca8d44845746562cf59487698eb73161b9 \
  --art-source builds/demo-candidate/src/art.cpp --observe

python3 bench/verify_core.py \
  --baseline builds/demo-core-original/art-probe \
  --candidate builds/demo-core-modified/art-probe \
  --output raw/demo-core-diff
```

原版与候选的 GoogleTest 构建使用 `--upstream-tests`，二进制名为 `art-tests`。`--gtest_filter=ArtTest.*` 运行 44 项原有 ART 测试；`--gtest_filter=ArtTraversalCutoffTest.*` 运行两项核心入口回归。精简构建显式排除未链接的迭代器入口测试，整套上游目标会包含它。

观测关闭时不加 `--observe`；调用图诊断使用 `--profile`；内存检查使用 `--sanitize --sanitizers address`。本环境的 ptrace 隔离与 LeakSanitizer 不兼容，ASan 验证在普通本地进程中执行。组合 UBSan 的原始失败报告保留在 `raw/core-ubsan-baseline.log` 和 `raw/core-sanitized-normal-local.log`。

本机组合检查可用 `--sanitize --pic` 重建原版 / 候选，并在普通本地进程中运行 `bench/verify_core.py`。已经通过的结果为 `raw/core-pic-sanitized-differential-20261028/summary.json`；这项结论只覆盖该 PIC 核心诊断配置，不覆盖尚未构建完成的整个服务。

## 完整服务器的后续验收

完整原版在干净源码下独立构建并保存，随后才应用补丁、构建候选。当前本地检出已应用候选；公开仓库的源码子模块仍固定在原版提交，通过下列补丁复现候选。检查、应用与回退入口如下：

```sh
git -C ../typesense apply --check ../retrieval-study/patches/0001-check-art-deadline-on-internal-nodes.patch
# 仅在原版二进制及哈希保存完成之后，再应用补丁：
git -C ../typesense apply ../retrieval-study/patches/0001-check-art-deadline-on-internal-nodes.patch
# 回退该补丁：
git -C ../typesense apply -R ../retrieval-study/patches/0001-check-art-deadline-on-internal-nodes.patch
```

`bench/build_server.py --label <新名称> --baseline` 负责干净原版构建，去掉 `--baseline` 则记录当前修改版。它保存开始与结束时的源码状态；构建中源码发生改变就不视为可追溯的成功基线。正式验收还需更广的共享路径回归、随机与并发混合负载，以及排除后台任务干扰的交替性能实验。

当前已完成完整构建、聚焦迭代器回归及首轮 HTTP 差分。原版目录为 `builds/v31-baseline-compat3-20261028/`，候选为 `builds/v31-candidate-compat3-20261028/`，每个目录有成功状态和二进制哈希。尚未运行全套上游 C++ 测试；正式并发、随机组合与性能验收仍待完成。

2026-10-28 的构建曾因旧 CMake 下载器的 DNS/TLS 错误失败。`bench/cache_onnx_downloads.py` 使用系统 curl 缓存依赖归档，校验上游 SHA-1 并额外记录 SHA-256，只将依赖 checkout 的下载地址改为本地文件，保留版本与 CMake 的原有哈希检查。原始下载清单和改写后的摘要保存在 `manifests/onnx-download-cache.json`；两个服务版本必须使用相同缓存配置。

本机 glibc 2.43 还需要 `bench/prepare_build_compat.py` 对 elfutils 0.191 的只读查找变量增加 `const`。原始文件哈希、差分与修正后哈希均记录；没有禁用警告。`build_server.py` 会核对共同依赖前后内容。此修正属于本机构建配置，不作为检索核心成果。

`bench/build_linked_art_tests.py` 使用成功服务的真实编译参数和生产对象，替换服务主程序以链接上游 ART 测试与三项新增回归，保留真实过滤实现。它提供聚焦集成验证，不能替代完整 `typesense-test`。原版 / 候选的结果分别见 `raw/v31-linked-{baseline,candidate}-tests-20261028.json`。

服务状态场景验证入口如下，覆盖普通 / 空 / OR 过滤、候选限制、分页、排序、精选 / 隐藏、facets、分组、多字段、多词、纠错、更新、删除重插和截止后恢复。有限截止只对照显式截止 / 408 契约，不要求两个版本返回相同部分结果。

```sh
python3 bench/validate_service.py --binary <完整服务器路径> \
  --source ../typesense --output raw/<新验证目录>
python3 bench/compare_service.py raw/<原版目录> raw/<修改版目录> \
  --output raw/<差分结果>.json
```

删除后的截止诊断使用 `bench/validate_deadline_service.py --binary <路径> --output raw/<新目录>`；对照使用 `bench/compare_deadline_service.py raw/<原版> raw/<候选> --output raw/<结果>.json`。原版 / 候选普通预算和恢复结果必须一致，有限预算只允许显式截止的部分结果。当前诊断每个用例每版 5 个样本，未报告正式吞吐、P99 或稳定加速比例。

