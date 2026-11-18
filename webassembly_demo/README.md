# WebAssembly 入门与实战

WebAssembly（Wasm）是一种可移植的二进制指令格式。开发者可以用 C、C++、Rust 或 AssemblyScript 编写程序，把它编译成 `.wasm` 模块，再交给浏览器或 Node.js 执行。例如，一个网页可以把商品图的像素交给已经编译好的 WebP 编码器，拿到压缩后的字节，生成可下载的图片。[MDN 对 WebAssembly 的介绍](https://developer.mozilla.org/zh-CN/docs/WebAssembly/Guides/Concepts)

这个目录有三个可以直接操作的例子。**结账优惠**演示如何编写和调用自己的模块，**素材压缩**和**订单报表**演示如何在网页里复用现成的编码器和数据库引擎。

## 运行示例

在仓库根目录执行以下命令。使用 Node.js 22.12 或更新版本和 npm 10 或更新版本。

```bash
cd webassembly_demo
npm ci
npm run dev
```

打开终端打印的本机地址。启动脚本会先编译折扣模块。页面自带一张示例海报和一份订单 CSV，三个例子都可以直接运行。首次安装需要下载依赖，之后示例从本地开发服务加载模块，图片和订单也在本机处理。

## 先看一次 Wasm 调用

在刚启动的开发页面打开浏览器控制台，执行下面两行，就能直接调用折扣函数。

```js
const { instance } = await WebAssembly.instantiateStreaming(fetch('./build/checkout.wasm'));
instance.exports.payableCents(3990n, 3); // 10773n，即 107.73 元
```

1. **编译。** AssemblyScript 编译器把折扣源码编译成 `build/checkout.wasm`。同时生成的 `build/checkout.wat` 是同一模块的文本表示，可以打开查看函数签名和乘除指令。[折扣源码](./assembly/checkout.ts) · [AssemblyScript 编译器文档](https://www.assemblyscript.org/compiler.html)
2. **实例化。** 浏览器下载模块，编译并创建可调用的实例。模块通过 exports 暴露函数，也可以通过 imports 接收宿主提供的能力。这个折扣函数只做整数运算，实例化时可以省略导入对象。[浏览器调用代码](./src/checkout-form.js)
3. **传值和取回结果。** 签名里的 `i64` 金额在 JavaScript 端对应 `BigInt`，所以 39.90 元写成 `3990n` 分。`i32` 件数直接传数字 `3`。处理图片这类成块数据时，编码库会把像素传入 Wasm 的线性内存，也就是模块能按字节读写的一段连续空间。[金额转换代码](./src/checkout.js) · [编码器调用代码](./src/encode.worker.js)

## 三个实战 demo

### 结账优惠 · 在网页和命令行复用规则

同款满 3 件打九折，满 10 件打八折。页面输入单价 `39.90`、件数 `3`，应看到商品小计 **119.70 元**、优惠 **11.97 元**、应付 **107.73 元**。把件数改成 `10`，应付变为 **319.20 元**。

在示例目录的另一个终端运行同一条规则。Node.js 从磁盘读取同一个模块，调用相同的导出函数。改动折扣源码后，重新运行命令即可编译。已启动的开发页面可用 `npm run build:wasm` 更新模块。[命令行入口](./cli/checkout.js)

```bash
npm run checkout -- 39.90 3
```

可以用单价 `0.05`、件数 `3` 检查舍入时机。整笔打折后是 0.135 元，最终应付 **0.14 元**。源码全程用整数分计算，只在整笔打折后四舍五入一次。

### 素材压缩 · 把海报或商品图导出为 WebP

点击「压缩为 WebP」，页面会显示预览、原始大小、编码后大小和下载链接。先用内置海报分别试试质量 `25` 和 `90`，观察文字边缘和渐变的变化，也可以选一张自己的静态 PNG、JPEG 或 WebP。

浏览器在 Web Worker 中解码图片，取得 RGBA 像素，再调用 jSquash 提供的 **libwebp Wasm 编码器**。编码器返回真正的 WebP 字节，页面把这些字节包装成可下载的文件。Worker 让编码过程在页面主线程之外执行。一张已经压缩过的原图，再次编码后也可能变大，可以直接比较两次显示的大小。[Worker 源码](./src/encode.worker.js) · [jSquash 项目](https://github.com/jamsinclair/jSquash)

### 订单报表 · 在浏览器里用 SQL 查询 CSV

点击「按城市汇总」。DuckDB-Wasm 在 Worker 中读取 6 笔示例订单，以十进制金额分组求和，按销售额降序返回下表。输入数据和查询语句都在目录里，可以各改一项来观察结果。[示例订单](./examples/orders.csv) · [汇总 SQL](./src/orders.sql)

| 城市 | 订单数 | 销售额（元） |
| --- | ---: | ---: |
| 杭州 | 2 | 200.00 |
| 深圳 | 2 | 170.00 |
| 上海 | 2 | 135.00 |

自选文件使用 UTF-8 编码和逗号分隔，第一行为表头。每行代表一笔订单，`city` 是城市名，`amount` 是非负金额，最多两位小数，额外列可以保留。试着把一笔金额改成 `12.345`，页面会指出金额格式问题。修正为 `12.35` 后重新选择文件，就可以再次汇总。[数据库加载与查询代码](./src/orders-engine.js) · [DuckDB-Wasm 项目介绍](https://duckdb.org/2021/10/29/duckdb-wasm)

## 观察加载成本

打开开发者工具的 Network 面板，筛选 `.wasm`，再刷新页面。初始只加载折扣模块（本示例编译后为 94 字节）。首次压缩时会加载 WebP 编码器，首次汇总时才会加载约 33 MiB 的 DuckDB 模块（未压缩大小）。Network 面板可以看到下载耗时。想观察编码或 SQL 查询占用的时间，可以在 Performance 面板开始录制，再运行一次示例并查看 Worker 线程。

要验证可部署的静态版本，在示例目录执行以下命令，打开预览服务打印的地址。构建产物在 `dist/`，包含页面、Workers 和 Wasm 模块。

```bash
npm run build
npm run preview
```
