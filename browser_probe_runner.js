const fs = require("fs");
const readline = require("readline");

function argument(name) {
    const index = process.argv.indexOf(name);
    return index >= 0 ? process.argv[index + 1] || "" : "";
}

const playwrightRoot = argument("--playwright-root");
const executablePath = argument("--executable");
const storageState = argument("--storage-state");
const engineName = argument("--engine") || "chromium";

let playwright;
let browser;

function writeResult(result) {
    process.stdout.write(`${JSON.stringify(result)}\n`);
}

function isBlockedUrl(url) {
    return /\/docs\/available-regions|available-regions/i.test(url);
}

function isBlockedText(text) {
    return /region not supported|account not supported|available regions for google ai studio|unsupported region/i.test(text);
}

function isGoogleApiUrl(url) {
    try {
        const host = new URL(url).hostname.toLowerCase();
        return host === "aistudio.google.com" || host.endsWith(".google.com") || host.endsWith(".googleapis.com");
    } catch (_error) {
        return false;
    }
}

async function probe(input) {
    const contextOptions = {
        proxy: { server: input.proxy },
        storageState: storageState && fs.existsSync(storageState) ? storageState : undefined,
    };
    const context = await browser.newContext(contextOptions);
    const page = await context.newPage();
    const failures = [];
    const pendingForbiddenReads = [];
    const started = Date.now();
    const targetUrl = `https://aistudio.google.com/prompts/new_chat?clash_switch_probe=${Date.now()}`;

    page.on("response", response => {
        if (response.status() !== 403 || !isGoogleApiUrl(response.url())) return;
        // 403 可能只是 API key 服务被禁用；只有响应正文明确说明地区/账号受限时，
        // 才把它归因到当前节点，避免所有节点被同一个全局 key 问题误判。
        const read = response.text().then(body => {
            if (isBlockedText(body)) failures.push(`${response.status()} ${response.url()}`);
        }).catch(() => {});
        pendingForbiddenReads.push(read);
    });

    try {
        try {
            await page.goto(targetUrl, {
                timeout: Math.max(5000, Math.min(12000, Number(input.timeoutMs) + 2000)),
                waitUntil: "domcontentloaded",
            });
        } catch (_error) {
            // 页面仍可能在导航超时后完成地区跳转，继续读取 URL 和正文。
        }

        const deadline = Date.now() + Math.max(5000, Math.min(12000, Number(input.timeoutMs) + 2000));
        let lastText = "";
        while (Date.now() < deadline) {
            const url = page.url();
            const title = await page.title().catch(() => "");
            lastText = await page.locator("body").innerText({ timeout: 500 }).catch(() => "");
            if (isBlockedUrl(url) || isBlockedText(`${title}\n${lastText}`) || failures.length > 0) {
                return {
                    ok: false,
                    blocked: true,
                    url,
                    title,
                    detail: failures[0] || "页面跳转到 AI Studio 地区不支持页",
                    elapsedMs: Date.now() - started,
                };
            }
            if (/accounts\.google\.com|servicelogin/i.test(url) || /sign in|登录/i.test(title)) {
                return {
                    ok: false,
                    blocked: false,
                    authenticated: false,
                    url,
                    title,
                    detail: "浏览器登录态已失效",
                    elapsedMs: Date.now() - started,
                };
            }
            if (Date.now() - started >= 6500 && /aistudio\.google\.com/i.test(url) && /google ai studio/i.test(title)) {
                await Promise.allSettled(pendingForbiddenReads);
                if (failures.length > 0) {
                    return {
                        ok: false,
                        blocked: true,
                        url,
                        title,
                        detail: failures[0],
                        elapsedMs: Date.now() - started,
                    };
                }
                return {
                    ok: true,
                    blocked: false,
                    authenticated: true,
                    url,
                    title,
                    elapsedMs: Date.now() - started,
                };
            }
            await new Promise(resolve => setTimeout(resolve, 200));
        }

        return {
            ok: false,
            blocked: false,
            url: page.url(),
            title: await page.title().catch(() => ""),
            detail: lastText ? "AI Studio 页面复核超时" : "AI Studio 页面未加载",
            elapsedMs: Date.now() - started,
        };
    } finally {
        await context.close().catch(() => {});
    }
}

async function main() {
    try {
        playwright = require(playwrightRoot || "playwright");
        const browserType = engineName === "firefox" ? playwright.firefox : playwright.chromium;
        const launchOptions = {
            headless: true,
            executablePath,
        };
        if (engineName !== "firefox") {
            launchOptions.args = ["--no-sandbox", "--disable-gpu", "--disable-extensions", "--disable-sync"];
        } else {
            launchOptions.args = ["-no-remote"];
        }
        browser = await browserType.launch(launchOptions);
        const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
        for await (const line of input) {
            if (!line.trim()) continue;
            try {
                writeResult(await probe(JSON.parse(line)));
            } catch (error) {
                writeResult({ ok: false, unavailable: false, detail: String(error && error.message ? error.message : error) });
            }
        }
    } catch (error) {
        writeResult({ ok: false, unavailable: true, detail: String(error && error.message ? error.message : error) });
        process.exitCode = 2;
    } finally {
        if (browser) await browser.close().catch(() => {});
    }
}

main();
