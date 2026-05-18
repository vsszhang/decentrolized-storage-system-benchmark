# Benchmark Phase 1 Update Notes

本文档记录本次针对二期工程项目的阶段 1 更新：基于现有 Python uv 项目骨架，完成 MinIO 基础 IO benchmark 的代码结构、配置、运行入口和测试用例。

## 1. 本次更新内容

本次更新将原来的最小 Python 项目扩展为标准 `src/` 结构，并实现了面向 S3 兼容对象存储的基础 IO 性能测试框架。

主要新增能力：

- 使用 `boto3` 访问 MinIO，后续可通过替换 endpoint 复用到 Ceph RGW。
- 支持 4 类基础 IO workload：
  - `seqwrite`：顺序写
  - `seqread`：顺序读
  - `randomwrite`：随机写
  - `randomread`：随机读
- 支持 smoke/full 两套配置：
  - `configs/minio-smoke.toml`：轻量验证配置，默认推荐使用。
  - `configs/minio-full.toml`：按报告规划包含 16KiB、10MiB、4GiB、10GiB 的完整规模配置。
- 每次运行输出结构化结果：
  - `metrics.csv`
  - `metrics.json`
  - `run_config.toml`
- 增加单元测试，覆盖配置加载、指标计算和 4 类 IO workload 调用逻辑。

## 2. 现有代码模型

当前代码按“配置 -> S3 客户端 -> workload 执行 -> sample 采集 -> metric 汇总 -> 结果输出”的流程组织。

运行流程如下：

1. CLI 读取 TOML 配置文件。
2. 从环境变量读取 MinIO/S3 连接信息。
3. 初始化 S3 兼容客户端。
4. 按配置顺序执行 workload。
5. 每次对象读写生成一条 `OperationSample`。
6. 汇总样本，计算吞吐量、平均延迟、P95/P99 延迟和 IOPS。
7. 将结果写入 `results/<timestamp>/`。

设计上的关键点：

- 对象存储访问通过 `S3Client` 协议抽象，避免 workload 直接绑定 boto3。
- 生成测试数据时使用 `DeterministicBytesStream` 流式生成确定性字节，不需要提前在内存中构造大对象。
- 读取对象时按 `chunk_size` 分块消费 S3 response body，适配 full 配置中的 4GiB/10GiB 大对象。
- `seqread` 和 `randomread` 依赖前置写入 workload 的 key 集合，通过 `source_workload` 显式声明数据来源。

## 3. 主要模块说明

`src/storage_benchmark/cli.py`

- 定义命令行入口 `storage-benchmark run`。
- 负责加载配置、读取环境变量、创建输出目录、调用 workload 执行器、写出结果。
- 当缺少 S3 环境变量时，会明确提示缺失项并退出。

`src/storage_benchmark/config.py`

- 定义配置模型：
  - `RunConfig`
  - `WorkloadConfig`
  - `BenchmarkConfig`
  - `S3Settings`
- 支持 `16KiB`、`10MiB`、`4GiB` 等容量字符串解析。
- 校验 workload 名称唯一性，以及读 workload 的 `source_workload` 是否存在。

`src/storage_benchmark/s3_client.py`

- 定义 `S3Client` 协议。
- 实现 `BotoS3Client`，封装 boto3 的 `put_object`、`get_object`、`delete_object`、`list_objects_v2`。
- 当前用于 MinIO，后续可用于 Ceph RGW。

`src/storage_benchmark/io_basic.py`

- 实现基础 IO workload：
  - `seqwrite`
  - `seqread`
  - `randomwrite`
  - `randomread`
- 实现 `DeterministicBytesStream`，用于流式生成测试对象内容。
- 实现 `run_workloads`，按配置顺序执行所有 workload。

`src/storage_benchmark/metrics.py`

- 定义单次操作样本 `OperationSample`。
- 定义汇总指标 `MetricRecord`。
- 计算：
  - 总操作数
  - 总字节数
  - 总耗时
  - 吞吐量 MB/s
  - 平均延迟 ms
  - P95/P99 延迟 ms
  - IOPS
- 输出 CSV 和 JSON。

`configs/minio-smoke.toml`

- 默认轻量配置。
- 包含：
  - 16KiB 小对象随机写/随机读，各 20 次。
  - 10MiB 中等对象顺序写/顺序读，各 1 次。

`configs/minio-full.toml`

- 完整测试配置。
- 包含：
  - 16KiB 小对象随机读写。
  - 10MiB 中等对象顺序读写。
  - 4GiB 大对象顺序读写。
  - 10GiB 大对象顺序读写。

`tests/`

- 使用 mock S3 client，不依赖真实 MinIO。
- 当前测试文件：
  - `tests/test_config.py`
  - `tests/test_metrics.py`
  - `tests/test_io_basic.py`

## 4. 如何运行脚本

### 4.1 安装依赖

```bash
uv sync --dev
```

### 4.2 设置 MinIO/S3 环境变量

```bash
export S3_ACCESS_KEY_ID="minioadmin"
export S3_SECRET_ACCESS_KEY="minioadmin"
```

当前 smoke/VPS 配置默认使用 `http://127.0.0.1:9000` 和 bucket `benchmark`。
只有当 MinIO 运行在 VPS 本机，或 VPN/SSH 隧道已将 MinIO 映射到 VPS 本地端口时，`127.0.0.1` 才是正确地址。

必填变量：

- `S3_ACCESS_KEY_ID`
- `S3_SECRET_ACCESS_KEY`

可选变量：

- `S3_ENDPOINT_URL`：覆盖配置文件中的 endpoint
- `S3_BUCKET`：覆盖配置文件中的 bucket
- `S3_REGION`：默认 `us-east-1`
- `S3_USE_SSL`：默认 `false`

### 4.3 运行 smoke benchmark

推荐先运行 smoke 配置，确认 MinIO 连通性和基础流程。

```bash
uv run storage-benchmark run --config configs/minio-smoke.toml
```

### 4.4 运行 full benchmark

full 配置会产生 4GiB 和 10GiB 对象，运行前需要确认网络、磁盘和 MinIO bucket 容量。

```bash
uv run storage-benchmark run --config configs/minio-full.toml
```

### 4.5 查看输出结果

每次运行会生成一个时间戳目录：

```text
results/<timestamp>/
  metrics.csv
  metrics.json
  run_config.toml
```

`metrics.csv` 适合后续做图表和报告分析，`metrics.json` 适合程序化读取。

## 5. 测试维度

当前单元测试覆盖以下维度。

### 5.1 配置加载与校验

文件：`tests/test_config.py`

覆盖内容：

- 容量字符串解析，例如 `16KiB`、`10MB`。
- `configs/minio-smoke.toml` 可正常加载。
- `configs/minio-full.toml` 包含报告要求的 16KiB、10MiB、4GiB、10GiB 测试规模。
- workload 操作类型能正确解析为内部枚举。
- 读 workload 能通过 `source_workload` 绑定前置写入 workload。

### 5.2 指标计算

文件：`tests/test_metrics.py`

覆盖内容：

- 操作数统计。
- 总字节数统计。
- 总耗时统计。
- 吞吐量 MB/s。
- 平均延迟 ms。
- P95/P99 延迟。
- IOPS。

### 5.3 基础 IO workload 行为

文件：`tests/test_io_basic.py`

覆盖内容：

- `seqwrite` 会调用 S3 `put_object`。
- `seqread` 会读取 `seqwrite` 生成的对象。
- `randomwrite` 会生成多个随机写入对象。
- `randomread` 会从前置随机写入对象集合中选择 key 并调用 S3 `get_object`。
- 测试使用 `FakeS3Client`，不依赖真实 MinIO 服务。

### 5.4 本地测试命令

```bash
uv run pytest
```

当前验证结果：

```text
5 passed
```

## 6. 后续扩展方向

后续阶段可以在当前结构上继续扩展：

- 增加 Ceph RGW 配置文件，只需要替换 S3 endpoint、bucket 和密钥。
- 增加 GeoTIFF/COG 读取模块，例如 `src/storage_benchmark/geotiff.py`。
- 增加 GDAL `read window`/tile-based access 测试。
- 增加结果分析模块，将 `metrics.csv` 转换为性能曲线。
- 增加并发读写配置，用于测试多 worker 场景下的吞吐和尾延迟。
