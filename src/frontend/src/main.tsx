import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "./App";
import { StandaloneDependencyChat } from "./components/StandaloneDependencyChat";
import "./styles.css";

const queryClient = new QueryClient();

// No router in this app (see StandaloneDependencyChat's comment) — a single
// query-param check here is the whole "route". Anything other than
// ?view=dependency-chat renders the normal tab-switching app unchanged.
const isStandaloneDependencyChat =
  new URLSearchParams(window.location.search).get("view") === "dependency-chat";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      {isStandaloneDependencyChat ? <StandaloneDependencyChat /> : <App />}
    </QueryClientProvider>
  </React.StrictMode>,
);
