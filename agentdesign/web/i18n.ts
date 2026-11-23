import { createContext, createElement, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

export type Locale = "en" | "zh";

export const LOCALE_STORAGE_KEY = "lore_locale";

let currentLocale: Locale = readStoredLocale();

function readStoredLocale(): Locale {
  try {
    return localStorage.getItem(LOCALE_STORAGE_KEY) === "zh" ? "zh" : "en";
  } catch {
    return "en";
  }
}

function applyDocumentLocale(locale: Locale) {
  if (typeof document === "undefined") return;
  document.documentElement.lang = locale === "zh" ? "zh-CN" : "en";
  document.title = locale === "zh" ? "LoRE 本地化工作台" : "LoRE Localization Workbench";
}

applyDocumentLocale(currentLocale);

export function getLocale(): Locale {
  return currentLocale;
}

export function pick(en: string, zh: string, locale: Locale = currentLocale): string {
  return locale === "zh" ? zh : en;
}

type LocaleContextValue = {
  locale: Locale;
  setLocale: (next: Locale) => void;
  toggleLocale: () => void;
  t: (en: string, zh: string) => string;
};

const LocaleContext = createContext<LocaleContextValue>({
  locale: currentLocale,
  setLocale: () => {},
  toggleLocale: () => {},
  t: (en, zh) => pick(en, zh),
});

export function LocaleProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(currentLocale);

  const setLocale = useCallback((next: Locale) => {
    currentLocale = next;
    try {
      localStorage.setItem(LOCALE_STORAGE_KEY, next);
    } catch {
      /* ignore storage errors */
    }
    applyDocumentLocale(next);
    setLocaleState(next);
  }, []);

  const toggleLocale = useCallback(() => {
    setLocale(currentLocale === "en" ? "zh" : "en");
  }, [setLocale]);

  useEffect(() => {
    applyDocumentLocale(locale);
  }, [locale]);

  const t = useCallback((en: string, zh: string) => (locale === "zh" ? zh : en), [locale]);

  return createElement(
    LocaleContext.Provider,
    { value: { locale, setLocale, toggleLocale, t } },
    children,
  );
}

export function useI18n() {
  return useContext(LocaleContext);
}
