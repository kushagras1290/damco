import { expect, test } from "@playwright/test";

// Runs against the full docker-compose stack (see Makefile `e2e`).

test("dashboard renders live data for the public demo", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  await expect(page.getByText("Eligible jobs")).toBeVisible();
});

test("every primary route loads without errors", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
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

test("proxy refuses paths outside the allowlist", async ({ page }) => {
  const response = await page.request.get("/api/backend/health/ready");
  expect(response.status()).toBe(404);
});
