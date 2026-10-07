import { expect, test } from "@playwright/test";

// Runs against the full docker-compose stack (see Makefile `e2e`).

test("dashboard renders live data for the public demo", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  await expect(page.getByText("Eligible jobs")).toBeVisible();
});

test("every primary route loads without errors or CSP violations", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error" && /Content Security Policy|Refused to/i.test(message.text())) {
      errors.push(message.text());
    }
  });
  for (const [path, heading] of [
    ["/jobs", "Jobs"],
    ["/sources", "Sources"],
    ["/decisions", "Eligibility decisions"],
    ["/runs", "Workflow runs"],
    ["/applications", "Applications"],
    ["/profile", "Profile & eligibility policy"],
    ["/system", "System"],
  ] as const) {
    await page.goto(path);
    await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible();
  }
  expect(errors).toEqual([]);
});

test("public demo cannot mutate data", async ({ page }) => {
  await page.goto("/sources");
  await expect(page.getByRole("button", { name: "Add source" })).toHaveCount(0);
  const response = await page.request.post("/api/backend/sources", {
    data: { kind: "greenhouse", name: "x", company_name: "x", company_domain: "x.io", board_token: "x" },
  });
  expect(response.status()).toBe(403);
});

test("pages carry a nonce-based CSP and auth errors are friendly", async ({ page }) => {
  const response = await page.goto("/dashboard");
  const csp = response?.headers()["content-security-policy"] ?? "";
  expect(csp).toMatch(/script-src 'self' 'nonce-[A-Za-z0-9+/=]+' 'strict-dynamic'/);
  expect(csp).not.toContain("'unsafe-inline' 'strict-dynamic'");
  await page.goto("/auth/error?error=AccessDenied");
  await expect(page.getByText("not an owner of this JobPulse instance")).toBeVisible();
});

test("cross-origin writes are blocked by the proxy", async ({ page }) => {
  const response = await page.request.post("/api/backend/sources", {
    headers: { origin: "https://evil.example" },
    data: { kind: "greenhouse", name: "x", company_name: "x", company_domain: "x.io", board_token: "x" },
  });
  expect(response.status()).toBe(403);
  expect((await response.json()).detail).toBe("cross-origin request blocked");
});

test("proxy refuses paths outside the allowlist", async ({ page }) => {
  const response = await page.request.get("/api/backend/health/ready");
  expect(response.status()).toBe(404);
});
