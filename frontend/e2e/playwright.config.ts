import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: '.',
  timeout: 45_000,
  expect: { timeout: 5_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['line'], ['json', { outputFile: 'e2e/results/playwright-report.json' }]],
  outputDir: 'e2e/results/test-output',
  use: {
    baseURL: 'http://127.0.0.1:8012',
    headless: true,
    viewport: { width: 1440, height: 1400 },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
})
