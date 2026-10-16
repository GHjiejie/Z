import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const fixture = (name: string) => fileURLToPath(new URL(`../fixtures/${name}`, import.meta.url));

test("enforces the cumulative limit and supports remove, retry and repeated selection", async ({ page }) => {
  await page.goto("/");
  const input = page.getByLabel("选择音视频文件");
  await input.setInputFiles(fixture("video-20.mp4"));
  await expect(page.getByTestId("video-total")).toHaveText("20.00");
  await input.setInputFiles(fixture("video-20.mp4"));
  await expect(page.getByRole("status").filter({ hasText: "重复文件" })).toBeVisible();
  await input.setInputFiles(fixture("video-10.mp4"));
  await expect(page.getByTestId("video-total")).toHaveText("30.00");
  await input.setInputFiles(fixture("video-over.mp4"));
  await expect(page.getByRole("article", { name: "video-over.mp4" })).toContainText("超过时长限制");
  await page.getByRole("button", { name: "移除 video-over.mp4" }).click();
  await page.getByRole("button", { name: "移除 video-10.mp4" }).click();
  await expect(page.getByTestId("video-total")).toHaveText("20.00");
  await input.setInputFiles(fixture("video-30.mp4"));
  const rejected = page.getByRole("article", { name: "video-30.mp4" });
  await expect(rejected).toContainText("超过时长限制");
  await page.getByRole("button", { name: "移除 video-20.mp4" }).click();
  await rejected.getByRole("button", { name: "重试" }).click();
  await expect(rejected).toContainText("已加入");
  await expect(page.getByTestId("video-total")).toHaveText("30.00");
});

test("overlapping selection batches cannot bypass the remaining budget", async ({ page }) => {
  await page.goto("/");
  const input = page.getByLabel("选择音视频文件");
  const buffer = await readFile(fixture("video-20.mp4"));
  await input.setInputFiles({ name: "first.mp4", mimeType: "video/mp4", buffer });
  await input.setInputFiles({ name: "second.mp4", mimeType: "video/mp4", buffer });
  await expect(page.getByRole("article", { name: "first.mp4" })).toContainText("已加入");
  await expect(page.getByRole("article", { name: "second.mp4" })).toContainText("超过时长限制");
  await expect(page.getByTestId("video-total")).toHaveText("20.00");
});

test("parses audio tracks and silent video, previews media and exports accepted metadata", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("选择音视频文件").setInputFiles([
    fixture("video-multi-audio.mp4"), fixture("audio-stereo.wav"), fixture("video-10.mp4"), fixture("audio-40.wav"),
  ]);
  const exportButton = page.getByRole("button", { name: "导出解析结果" });
  await expect(exportButton).toBeEnabled();
  const video = page.getByRole("article", { name: "video-multi-audio.mp4" });
  await video.getByText("查看音轨与预览", { exact: true }).click();
  await expect(video).toContainText("2 条音轨");
  await expect(video.getByText("2 声道", { exact: true })).toHaveCount(2);
  await expect(video.getByText("48 kHz", { exact: true })).toHaveCount(2);
  await expect(video.locator("video")).toBeVisible();
  await expect.poll(() => video.locator("video").evaluate((element) => element instanceof HTMLVideoElement ? element.readyState : 0)).toBeGreaterThanOrEqual(1);
  const silent = page.getByRole("article", { name: "video-10.mp4" });
  await silent.getByText("查看音轨与预览", { exact: true }).click();
  await expect(silent).toContainText("此视频没有音轨");
  const audio = page.getByRole("article", { name: "audio-stereo.wav" });
  await audio.getByText("查看音轨与预览", { exact: true }).click();
  await expect(audio.locator("audio")).toBeVisible();
  await expect(audio).toContainText("不计入视频时长");
  await expect(page.getByTestId("video-total")).toHaveText("15.00");
  const downloadPromise = page.waitForEvent("download");
  await exportButton.click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("media-metadata.json");
  const path = await download.path();
  if (!path) throw new Error("Expected a local download path");
  const exported = JSON.parse(await readFile(path, "utf8"));
  expect(exported).toHaveLength(4);
  expect(exported[0].audioTracks).toHaveLength(2);
  expect(exported[1].audioTracks[0].channels).toBe(2);
  expect(exported[3].durationSeconds).toBe(40);
});

test("rejects corrupt and empty files and determines type from actual contents", async ({ page }) => {
  await page.goto("/");
  const input = page.getByLabel("选择音视频文件");
  await input.setInputFiles([fixture("corrupt.mp4"), fixture("empty.mp4")]);
  await expect(page.getByText("解析失败", { exact: true })).toHaveCount(2);
  await expect(page.getByRole("button", { name: "导出解析结果" })).toBeDisabled();
  await expect(page.getByRole("article", { name: "empty.mp4" })).toContainText("文件为空");
  await input.setInputFiles({ name: "renamed.txt", mimeType: "text/plain", buffer: await readFile(fixture("audio-stereo.wav")) });
  await expect(page.getByRole("article", { name: "renamed.txt" })).toContainText("已加入");
  await expect(page.getByTestId("video-total")).toHaveText("0.00");
});

test("loads WebM metadata through the production WASM bundle without external requests", async ({ page }) => {
  const externalRequests: string[] = [];
  page.on("request", (request) => {
    if (/^https?:/.test(request.url()) && !request.url().startsWith("http://127.0.0.1:5189/")) externalRequests.push(request.url());
  });
  await page.goto("/");
  await page.getByLabel("选择音视频文件").setInputFiles(fixture("video.webm"));
  const video = page.getByRole("article", { name: "video.webm" });
  await expect(video).toContainText("已加入");
  await expect(video).toContainText("1 条音轨");
  await video.getByText("查看音轨与预览", { exact: true }).click();
  await expect(video).toContainText("Opus");
  expect(externalRequests).toEqual([]);
});

test("accepts a dragged audio file", async ({ page }) => {
  await page.goto("/");
  const bytes = Array.from(await readFile(fixture("audio-stereo.wav")));
  const transfer = await page.evaluateHandle((buffer) => {
    const data = new DataTransfer();
    data.items.add(new File([new Uint8Array(buffer)], "dropped.wav", { type: "audio/wav" }));
    return data;
  }, bytes);
  await page.locator(".drop-zone").dispatchEvent("drop", { dataTransfer: transfer });
  await expect(page.getByRole("article", { name: "dropped.wav" })).toContainText("已加入");
  await expect(page.getByTestId("video-total")).toHaveText("0.00");
});

test("reports a parser loading failure and recovers on retry", async ({ page }) => {
  let fail = true;
  await page.route("**/*.wasm", async (route) => {
    if (fail) await route.abort();
    else await route.continue();
  });
  await page.goto("/");
  await page.getByLabel("选择音视频文件").setInputFiles(fixture("video-10.mp4"));
  const video = page.getByRole("article", { name: "video-10.mp4" });
  await expect(video).toContainText("解析器加载失败");
  fail = false;
  await video.getByRole("button", { name: "重试" }).click();
  await expect(video).toContainText("已加入");
  await expect(page.getByTestId("video-total")).toHaveText("10.00");
});

test("clearing during parsing cancels stale results and permits a new selection", async ({ page }) => {
  await page.route("**/*.wasm", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 500));
    await route.continue();
  });
  await page.goto("/");
  const input = page.getByLabel("选择音视频文件");
  await input.setInputFiles([fixture("video-20.mp4"), fixture("video-10.mp4")]);
  await page.getByRole("button", { name: "清空列表" }).click();
  await expect(page.getByRole("article")).toHaveCount(0);
  await input.setInputFiles(fixture("audio-stereo.wav"));
  await expect(page.getByRole("article", { name: "audio-stereo.wav" })).toContainText("已加入");
  await expect(page.getByRole("article")).toHaveCount(1);
  await expect(page.getByTestId("video-total")).toHaveText("0.00");
});

test("keeps empty and populated layouts within a narrow viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "音视频上传与解析。" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByLabel("选择音视频文件").setInputFiles({
    name: "这是一个很长的文件名称用于验证手机布局不会被视频文件名称撑开.mp4",
    mimeType: "video/mp4",
    buffer: await readFile(fixture("video-multi-audio.mp4")),
  });
  await expect(page.getByText("已加入", { exact: true })).toBeVisible();
  await page.getByText("查看音轨与预览", { exact: true }).click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: "test-results/mobile.png", fullPage: true });
});
