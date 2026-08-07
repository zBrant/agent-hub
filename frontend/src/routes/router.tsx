import { createBrowserRouter, Navigate } from "react-router";
import { DashboardRoute } from "@/routes/DashboardRoute";
import { NotFoundRoute } from "@/routes/NotFoundRoute";
import { RootLayout } from "@/routes/RootLayout";
import { SearchRoute } from "@/routes/SearchRoute";
import { SessionRoute } from "@/routes/SessionRoute";
import { SessionsIndexRoute } from "@/routes/SessionsIndexRoute";

/** docs/conventions.md §1: /dashboard, /sessions/:id, /search. */
export const router = createBrowserRouter([
  {
    path: "/",
    Component: RootLayout,
    children: [
      { index: true, element: <Navigate replace to="/dashboard" /> },
      { path: "dashboard", Component: DashboardRoute },
      { path: "sessions", Component: SessionsIndexRoute },
      { path: "sessions/:id", Component: SessionRoute },
      { path: "search", Component: SearchRoute },
      { path: "*", Component: NotFoundRoute },
    ],
  },
]);
