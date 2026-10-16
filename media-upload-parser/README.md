# 前端音视频上传与解析

独立的 React + TypeScript 示例，使用 `mediainfo.js` 的 WebAssembly 在浏览器中解析音视频文件，并在加入上传队列前检查所有视频的累计时长是否超过 **30 秒**。

## 启动

需要 Node.js 20.19+ 或 22.12+（推荐受支持的 LTS 版本）。

```bash
cd /Users/zhengjie/Github/Z/media-upload-parser
npm install
npm run dev
```

打开 <http://127.0.0.1:5188>。可以选择多个文件，也可以拖拽文件到上传区域。

```bash
npm run typecheck
npm test
npm run build
npm run preview
```

构建后部署 `dist/` 即可，包含本地 WASM 资源，无需 CDN 或后端解析服务。请通过 HTTP/HTTPS 访问，不能直接双击打开 `index.html`。

## 已实现的行为

- 多文件选择、拖拽、分批追加，按文件名、大小与修改时间跳过重复选择。
- 读取文件真实内容识别音频/视频，文件后缀和 MIME 类型仅用于选择器提示。
- 读取总时长、容器格式、视频编码、分辨率、帧率，以及每一条音轨的编码、语言、声道数、采样率、码率、时长和默认标记。文件未提供的字段显示“未提供”。**音轨数量和声道数量是不同概念**：一个双声道音轨仍然只有一条音轨。
- 无音轨的视频可正常加入，并显示“此视频没有音轨”。
- 浏览器原生音视频预览；浏览器无法解码的格式仍可显示解析到的元数据。
- 逐个解析并累计校验，防止分批快速选择绕过额度。使用 Worker 和 `File.slice()` 分块读取，避免为了获取元数据将整个文件解码进主线程。
- 解析中、已加入、超过时长限制、解析失败四种状态；支持移除、清空、重试。
- 取消后终止当前 Worker，忽略过期结果；超时、空文件、损坏文件或缺失有效时长均不能加入上传队列。
- 导出已加入文件的 JSON 解析结果；解析未完成时禁用导出。
- 提供 `createUploadFormData()`，用于接入真实上传接口，生成前再次校验总时长。

## 30 秒规则

当前按**所有已加入视频的累计时长**执行限制。音频文件单独解析，不占用视频额度。

| 当前已有视频 | 新选择的文件 | 结果 |
| --- | --- | --- |
| 无 | 30 秒视频 | 加入 |
| 无 | 30.001 秒视频 | 拒绝 |
| 20 秒视频 | 10 秒视频 | 加入，累计 30 秒 |
| 20 秒视频 | 10.001 秒视频 | 拒绝，已有视频保留 |
| 30 秒视频 | 40 秒音频 | 加入，视频累计仍为 30 秒 |

同一批文件按选择顺序处理，优先保留能加入的文件；超限文件不会占用额度，后续较短视频仍可尝试加入。超限文件保留提示，删除其他视频释放额度后，可以点击该文件的“重试”。

对于一个视频文件，取容器、视频轨和音轨中已知的**最长有效时长**，防止短视频轨掩盖更长的音频轨。MediaInfo 的 object/JSON 输出中 `Duration` 的单位为秒，不能再除以 1000。

界面显示时会格式化小数，实际校验使用解析得到的精度，通过整数皮秒累计避免浮点求和误差，既不把 `29.9 + 0.1` 错判为超限，也不把 `30.0001` 四舍五入成 30 后放行。

在 [src/media/config.ts](src/media/config.ts) 中调整业务上限：

```ts
export const MAX_VIDEO_DURATION_SECONDS = 30;
```

## 接入业务项目

页面当前完成**本地选择、解析与校验**，没有配置服务端上传地址，也没有发送文件。接口确定后，把校验通过的文件提交给业务接口：

```ts
import { createUploadFormData } from "./media/validation";
import type { MediaEntry } from "./media/types";

async function upload(entries: MediaEntry[], endpoint: string, signal?: AbortSignal) {
  // 再次检查：没有解析中的文件、存在有效文件、视频累计不超限。
  const body = createUploadFormData(entries);
  const response = await fetch(endpoint, {
    method: "POST",
    body,
    signal,
  });
  if (!response.ok) throw new Error(`上传失败：HTTP ${response.status}`);
  return response;
}
```

表单包含重复的 `files` 字段，以及一个 `metadata` JSON 字段；二者顺序一致。`metadata` 中每项包含 `id`、文件名、大小、媒体类型、时长和轨道信息。后端字段名、鉴权、大小限制与响应格式需按实际 API 契约对接。提交 `FormData` 时让浏览器生成带 boundary 的 `Content-Type`。

已有项目只需要核心能力时，可以复用 `src/media/`；其中 `useMediaUpload.ts` 依赖 React，解析器和校验函数可独立使用：

```ts
import { MediaParser } from "./media/parser";
import { validateAddition } from "./media/validation";

const parser = new MediaParser();
const controller = new AbortController();

try {
  const metadata = await parser.parse(file, controller.signal);
  const error = validateAddition(existingEntries, metadata);
  if (error) throw new Error(error);
  console.log(metadata.durationSeconds, metadata.audioTracks);
} finally {
  parser.dispose();
}
// 解析过程中需要取消时调用 controller.abort()。
```

迁移到其他打包器时，要同时迁移 Worker 和 `MediaInfoModule.wasm` 的 URL 配置。

## 验证

单元测试覆盖累计 30 秒边界、浮点误差、极小超限、无效时长、音频排除、音轨解析和提交前复核。

浏览器测试使用 FFmpeg 生成的本地真实素材，覆盖多音轨 MP4、无音轨视频、WebM、WAV、损坏/空文件、重复选择、分批累计、移除重试、拖拽、解析器加载失败与重试、解析取消、JSON 导出和窄屏布局。测试默认针对**生产构建**运行，验证 Worker 和 WASM 打包后仍可正常加载；设置 `MEDIA_E2E_DEV=1` 可以针对强制重新预构建依赖的开发服务器运行，验证首次选择文件也能正常解析。

运行浏览器测试需要本地 FFmpeg（包含 `libx264`、`libvpx-vp9`、`libopus`、AAC 编码器）和 Chromium：

```bash
npx playwright install chromium
npm run test:e2e
```

如果 FFmpeg 不在 PATH 中：

```bash
FFMPEG_PATH=/absolute/path/to/ffmpeg npm run test:e2e
```

测试素材自动生成到 `tests/fixtures/`，不提交生成的媒体文件、报告或构建产物。

## 能力边界

- 本示例读取音轨信息，不提取、转码或下载独立音轨。
- 常见容器能否解析取决于 MediaInfo 支持情况和文件完整性；能否预览还取决于浏览器解码支持。不能把“预览失败”直接等同于“没有音轨”。
- 时长以文件中可解析的元数据为准。正式服务端上传仍应复核文件内容与时长，客户端检查用于提前反馈。
- 这里没有额外设置文件大小限制；生产环境可根据真实接口契约补充。

相关文档：[MediaInfo 使用方式](https://mediainfo.js.org/docs/getting-started/usage/)、[WASM 加载方式](https://mediainfo.js.org/docs/getting-started/installation/)、[浏览器原生 audioTracks 兼容性](https://developer.mozilla.org/en-US/docs/Web/API/HTMLMediaElement/audioTracks)。
