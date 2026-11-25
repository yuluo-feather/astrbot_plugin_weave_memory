import { reactive } from "vue";
import { api, Capabilities, GroupItem } from "./api/weave";

export const state = reactive({
  capabilities: null as Capabilities | null,
  groups: [] as GroupItem[],
  groupIndex: 0,
  loaded: false,
  error: "",
});

export function currentGroup(): GroupItem | null {
  return state.groups[state.groupIndex] ?? null;
}

export async function loadBase(): Promise<void> {
  state.error = "";
  try {
    state.capabilities = await api.capabilities();
  } catch (error) {
    state.error = String(error);
  }
  try {
    state.groups = (await api.groups()).items;
  } catch (error) {
    state.error = state.error || String(error);
  }
  state.loaded = true;
}
