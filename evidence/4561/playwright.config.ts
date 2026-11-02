import { defineConfig } from '@playwright/test';
export default defineConfig({testDir:'.',testMatch:process.env.AUDIT_SPEC || 'audit.flow.ts',workers:1,reporter:'list',use:{baseURL:process.env.FLOW_BASE_URL,viewport:{width:1280,height:1000}},outputDir:'./test-results'});
