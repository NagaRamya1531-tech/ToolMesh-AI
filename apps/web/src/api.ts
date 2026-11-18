type DependencyStatus = "available" | "unavailable";

export interface Readiness {
  status: "ready" | "unready";
  checked_at: string;
  checks: { postgres: DependencyStatus; redis: DependencyStatus };
}

export async function getReadiness(signal: AbortSignal): Promise<Readiness> {
  const response = await fetch("/readyz", { signal });
  if (response.status !== 200 && response.status !== 503) {
    throw new Error("The gateway could not be reached.");
  }
  const data = await response.json();
  const isCheck = (value: unknown) => value === "available" || value === "unavailable";
  if (
    data?.status !== (response.ok ? "ready" : "unready") ||
    typeof data.checked_at !== "string" ||
    Number.isNaN(Date.parse(data.checked_at)) ||
    !isCheck(data.checks?.postgres) ||
    !isCheck(data.checks?.redis)
  ) {
    throw new Error("The gateway returned an invalid readiness response.");
  }
  return data;
}

export async function call<T>(path: string, token?: string, body?: object, method?: "PATCH"): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    method: method ?? (body === undefined ? "GET" : "POST"),
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.error?.message ?? `Gateway request failed (${response.status})`);
  }
  return response.status === 204 ? undefined as T : response.json();
}
