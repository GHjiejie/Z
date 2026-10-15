import { expect, test } from "@playwright/test";
import { EventSchema } from "@ag-ui/core/schemas";

// Explicitly opt into the disposable smoke app; never log into a user's real
// deployment or invoke a paid model with fixture credentials.
test.skip(process.env.PLATFORM_AG_UI_SMOKE !== "1", "Requires tests.ag_ui_smoke_app on a separate loopback port");

test("真实前后端 AG-UI：工具调用、刷新恢复、取消和费用凭据", async ({ page, context }, info) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const login = await context.request.post("/api/v1/auth/login", { data: { email: "ag-ui-browser@example.test", password: "temporary-platform-fixture-password" } });
  expect(login.status()).toBe(200);
  const auth = await login.json();
  const tenant = auth.user.memberships.find((item: { role: string }) => item.role === "owner").tenant_id as string;
  const root = `/api/v2/tenants/${tenant}`;
  await page.goto(`/#playground?tenant=${tenant}`);
  const agent = page.getByRole("combobox", { name: "当前 Agent", exact: true });
  await agent.click();
  await page.getByRole("option", { name: /AG-UI 联调助手/ }).click();
  const input = page.getByRole("textbox", { name: "发送给 Agent 的消息" });
  await input.fill("请计算 2+3");
  await input.press("Enter");
  await expect(page.getByText("计算结果是 5。", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "停止生成" })).toHaveCount(0);
  await page.getByRole("button", { name: "运行详情", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "运行详情" })).toContainText("正在调用：calculator");
  await expect(page.getByRole("dialog", { name: "运行详情" })).toContainText("工具已返回结果");
  await expect(page.getByRole("dialog", { name: "运行详情" })).toContainText("运行完成");
  await page.keyboard.press("Escape");
  const calls = await (await context.request.get(`${root}/usage/calls`)).json();
  expect(calls.items).toHaveLength(2);
  expect(calls.items.every((call: { status: string }) => call.status === "confirmed")).toBe(true);
  const completedRun = calls.items[0].run_id as string;
  const replay = await context.request.get(`${root}/runs/${completedRun}/ag-ui`);
  expect(replay.status()).toBe(200);
  expect(replay.headers()["x-ag-ui-protocol-version"]).toBe("1.0");
  const events = (await replay.text()).split("\n").filter((line) => line.startsWith("data: ")).map((line) => EventSchema.parse(JSON.parse(line.slice(6))));
  expect(events[0].type).toBe("RUN_STARTED");
  expect(events.at(-1)?.type).toBe("RUN_FINISHED");
  expect(events.some((event) => event.type === "TOOL_CALL_ARGS")).toBe(true);
  await input.fill("等待取消");
  await input.press("Enter");
  await expect(page.getByRole("article", { name: "Agent 正在回复" })).toContainText("正在等待取消。");
  const sessionUrl = page.url();
  await page.reload();
  await expect(page).toHaveURL(sessionUrl);
  await expect(page.getByRole("article", { name: "Agent 正在回复" })).toContainText("正在等待取消。");
  const resumedCalls = await (await context.request.get(`${root}/usage/calls`)).json();
  expect(resumedCalls.items).toHaveLength(3);
  await page.getByRole("button", { name: "停止生成" }).click();
  await expect(page.getByRole("button", { name: "停止生成" })).toHaveCount(0);
  const runs = await (await context.request.get(`${root}/runs`)).json();
  expect(runs.items[0].status).toBe("cancelled");
  await expect(input).toBeEnabled();
  await page.screenshot({ path: info.outputPath("ag-ui-real-frontend-backend.png"), fullPage: true });
  expect(errors).toEqual([]);
  await context.request.post("/api/v1/auth/logout", { headers: { "X-CSRF-Token": auth.csrf_token } });
});
