import { setLocale, t, useLocale, type Locale } from "./index";

export function LanguageSelector() {
  const locale = useLocale();
  return (
    <label className="field compact languageSelector">
      {t("Language")}
      <select aria-label={t("Language")} value={locale} onChange={(event) => setLocale(event.target.value as Locale)}>
        <option value="en" lang="en">English</option>
        <option value="zh-CN" lang="zh-CN">简体中文</option>
      </select>
    </label>
  );
}
