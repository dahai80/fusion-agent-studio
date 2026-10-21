import { useCallback, useEffect, useState } from 'react';
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
    // #418: t must be a stable identity per lang. A fresh function per
    // render made every effect with t in its deps re-run each render, and
    // effects that setState on fetch turned that into a refetch storm
    // (~4300 req/s on canvas/runs).
    return useCallback((zh: string, en: string) => (lang === 'en' ? en : zh), [lang]);
}
