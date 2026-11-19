export const consoleSections = [
  { label: "Platform", tone: "platform", pages: [
    ["Overview", "/"], ["Services", "/services"], ["Registry", "/tools"], ["AutoCrawler", "/crawler"],
  ] },
  { label: "Access", tone: "access", pages: [
    ["Agents", "/agents"], ["Policies", "/policies"], ["Approvals", "/approvals"],
  ] },
  { label: "Operations", tone: "operations", pages: [
    ["Playground", "/playground"], ["Executions", "/executions"], ["Audit", "/audit"],
  ] },
  { label: "Quality", tone: "quality", pages: [["Evaluations", "/evaluations"]] },
  { label: "Administration", tone: "administration", pages: [["Settings", "/settings"]] },
] as const;

export function consoleSection(page: string) {
  const parent = ["Agent scenarios", "Duplicate payment workflow"].includes(page) ? "Playground" : page;
  return consoleSections.find(({ pages }) => pages.some(([name]) => name === parent));
}
