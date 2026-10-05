import { useSyncExternalStore } from "react";
import { appMessages } from "./app.messages";
import { managementMessages } from "./management.messages";
import { summaryMessages } from "./summary.messages";
import { apiMessages } from "./api.messages";

export type Locale = "en" | "zh-CN";
export type Message = Record<Locale, string>;
export const DEFAULT_LOCALE: Locale = "en";
export const LOCALE_STORAGE_KEY = "seaweedfs-console.locale";

export const messages: Record<string, Message> = {
  ...apiMessages,
  ...summaryMessages,
  ...managementMessages,
  ...appMessages,
  Language: { en: "Language", "zh-CN": "语言" },
};

export function resolveLocale(value: unknown): Locale {
  return value === "zh-CN" ? "zh-CN" : DEFAULT_LOCALE;
}

function readStoredLocale(): Locale {
  try {
    return resolveLocale(typeof window === "undefined" ? null : window.localStorage.getItem(LOCALE_STORAGE_KEY));
  } catch {
    return DEFAULT_LOCALE;
  }
}

let locale = readStoredLocale();
const listeners = new Set<() => void>();

function updateDocumentLanguage() {
  if (typeof document !== "undefined") document.documentElement.lang = locale;
}

function applyLocale(next: Locale) {
  if (locale === next) return;
  locale = next;
  updateDocumentLanguage();
  listeners.forEach((listener) => listener());
}

updateDocumentLanguage();
if (typeof window !== "undefined") {
  window.addEventListener("storage", (event) => {
    if (event.key === LOCALE_STORAGE_KEY || event.key === null) applyLocale(resolveLocale(event.newValue));
  });
}

export function getLocale(): Locale {
  return locale;
}

export function setLocale(value: Locale) {
  const next = resolveLocale(value);
  // Switching still works when browser privacy settings disallow persistence.
  try {
    if (typeof window !== "undefined") window.localStorage.setItem(LOCALE_STORAGE_KEY, next);
  } catch {
    // Keep the preference in memory for this page.
  }
  applyLocale(next);
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function useLocale(): Locale {
  return useSyncExternalStore(subscribe, getLocale, () => DEFAULT_LOCALE);
}

type Params = Record<string, string | number>;
function interpolate(value: string, params: Params) {
  return value.replace(/\{(\w+)\}/g, (token, name: string) => Object.hasOwn(params, name) ? String(params[name]) : token);
}

/** Translate owned UI copy only. Object names, keys and raw DTOs stay unchanged. */
export function t(source: string, params: Params = {}): string {
  const message = messages[source];
  return interpolate(message?.[locale] || message?.en || source, params);
}

function compileMessages(catalog: Record<string, Message>) {
  const aliases = new Map<string, Message>();
  for (const message of Object.values(catalog)) {
    aliases.set(message.en, message);
    aliases.set(message["zh-CN"], message);
  }
  const templates = Object.entries(catalog).flatMap(([source, message]) => [...new Set([source, message.en, message["zh-CN"]])].flatMap((text) => {
  const names: string[] = [];
  const pattern = text.split(/(\{\w+\})/g).map((part) => {
    const placeholder = /^\{(\w+)\}$/.exec(part);
    if (placeholder) {
      names.push(placeholder[1]);
      return "([\\s\\S]+?)";
    }
    return part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }).join("");
  return names.length ? [{ message, names, pattern: new RegExp(`^${pattern}$`) }] : [];
  }));
  return { catalog, aliases, templates };
}

const uiNotices = compileMessages(messages);
const apiNotices = compileMessages({
  ...apiMessages,
  SESSION_EXPIRED: appMessages.SESSION_EXPIRED,
  STALE_AUTH_RESPONSE: appMessages.STALE_AUTH_RESPONSE,
  INITIAL_UNAUTHENTICATED: appMessages.INITIAL_UNAUTHENTICATED,
});

function renderNotice(value: string, notices: ReturnType<typeof compileMessages>): string {
  const message = Object.hasOwn(notices.catalog, value) ? notices.catalog[value] : notices.aliases.get(value);
  if (message) return message[locale] || message.en;
  for (const template of notices.templates) {
    const match = template.pattern.exec(value);
    if (!match) continue;
    const params = Object.fromEntries(template.names.map((name, index) => [name, /^(error|message|detail)$/.test(name) ? translateApiMessage(match[index + 1]) : match[index + 1]]));
    return interpolate(template.message[locale] || template.message.en, params);
  }
  return value;
}

function translateApiMessage(value: string): string {
  const error = /^([A-Z][A-Z0-9_]*): ([\s\S]*)$/.exec(value);
  if (error) return `${error[1]}: ${translateApiMessage(error[2])}`;
  return renderNotice(value, apiNotices);
}

/** Render stored API/local notices in the current language, retaining error codes. */
export function translateMessage(value: string): string {
  const error = /^([A-Z][A-Z0-9_]*): ([\s\S]*)$/.exec(value);
  if (error) return translateApiMessage(value);
  return renderNotice(value, uiNotices);
}

export function formatNumber(value: number, options?: Intl.NumberFormatOptions): string {
  return new Intl.NumberFormat(locale, options).format(value);
}

export function formatDate(value: string | number, options?: Intl.DateTimeFormatOptions): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat(locale, options ?? { dateStyle: "medium", timeStyle: "short" }).format(date);
}
