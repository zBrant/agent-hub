import { NavLink } from "react-router";
import { cn } from "@/lib/utils";

/** The three tabs of design.md §8. Routing is the tab state — no local state. */
const TABS = [
  { to: "/dashboard", label: "Dashboard" },
  { to: "/sessions", label: "Sessions" },
  { to: "/search", label: "Search" },
] as const;

export function TabNav() {
  return (
    <nav aria-label="Main" className="flex items-center gap-1">
      {TABS.map((tab) => (
        <NavLink
          key={tab.to}
          to={tab.to}
          className={({ isActive }) =>
            cn(
              "flex h-8 items-center rounded-md px-3 text-ui transition-colors duration-[120ms] ease-out",
              isActive
                ? "bg-elevated text-fg"
                : "text-fg-muted hover:bg-elevated hover:text-fg",
            )
          }
        >
          {tab.label}
        </NavLink>
      ))}
    </nav>
  );
}
