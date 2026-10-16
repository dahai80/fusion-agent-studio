import { useEffect, useState } from 'react';
import { sdk } from './sdk';

// Lang state bound to the SDK i18n channel (host pushes 'lang' events;
// standalone falls back to <html lang>). All module strings route through
// t() so the host language switch re-renders the module.
export function useLang(): 'zh' | 'en' {
    const [lang, setLang] = useState<'zh' | 'en'>(sdk.i18n.lang());
    useEffect(() => sdk.i18n.subscribe((next) => setLang(next)), []);
    return lang;
}

export function useT() {
    const lang = useLang();
    return (zh: string, en: string) => (lang === 'en' ? en : zh);
}
