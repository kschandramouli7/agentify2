import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev-server proxy target.
//
// The default is LOCAL on purpose. This used to hard-code a live ALB hostname,
// which silently rotted: ALB names are account-specific, so after the migration
// from 175920682311 to 637423369012 the name stopped resolving at all
// (confirmed 2026-09-01 — DNS returns NXDOMAIN) and `npm run dev` proxied every
// request into a black hole with no hint as to why. A stale default that fails
// this quietly is worse than no default.
//
// Point it at a live cluster explicitly instead:
//
//   # via a port-forward (see docs/CLOUDSHELL_RUNBOOK.md)
//   kubectl port-forward -n agentify svc/agentify-backend 18080:8080 &
//   VITE_BACKEND_URL=http://localhost:18080 npm run dev
//
//   # or straight at the ALB — read the CURRENT hostname, never a copy:
//   #   kubectl get ingress agentify -n agentify \
//   #     -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'
//   VITE_BACKEND_URL=http://<that-hostname> npm run dev
const BACKEND = process.env.VITE_BACKEND_URL || "http://localhost:8080";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api":    { target: BACKEND, changeOrigin: true },
      "/admin":  { target: BACKEND, changeOrigin: true },
      "/metrics":{ target: BACKEND, changeOrigin: true },
      "/admin/tracked": { target: BACKEND, changeOrigin: true },
    },
  },
});
