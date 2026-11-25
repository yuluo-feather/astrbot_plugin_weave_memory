import { reactive } from "vue";

export const toast = reactive({
  show: false,
  text: "",
  color: "success" as "success" | "error" | "warning",
});

export function notify(text: string, color: "success" | "error" | "warning" = "success"): void {
  toast.text = text;
  toast.color = color;
  toast.show = true;
}

export function reportError(error: unknown): void {
  const text = error instanceof Error ? error.message : String(error);
  notify(text || "操作失败", "error");
}
