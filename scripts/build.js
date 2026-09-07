// -- KATEGORIEN (angepasst nach deiner M3U) --------------------------------

const CATEGORY_RULES = [
  {
    name: "Ulusal",
    re: /\b(TRT|MECLİS|TABII|SHOW|STAR|ATV|KANAL D|NOW TV|EXXEN|TV ?8|TEVE 2|BEYAZ|360|SKY 360|A2 TV|EURO D|KANAL 7|DMAX TURKIYE|BENGUTÜRK|ULUSAL)\b/i,
  },
  {
    name: "Haber",
    re: /\b(HABER|NEWS|CNN|NTV|A HABER|BLOOMBERG|HALK TV|SÖZCÜ|LIDER|FLASH|GLOBAL|TV 100|TGRT HABER|ÜLKE|DHA|KANAL B|KANAL 24|TV NET|AKIT|ANADOLU)\b/i,
  },
  {
    name: "Belgesel",
    re: /\b(BELGESEL|DOKU|DOCU|DISCOVERY|NATIONAL GEOGRAPHIC|NAT GEO|HISTORY|ANIMAL PLANET|BBC EARTH|TLC|DMAX|TRT BELGESEL|TGRT BELGESEL|VIASAT|DA VINCI|DOCUBOX|FASHION|BEIN IZ|GURME)\b/i,
  },
  {
    name: "Spor",
    re: /\b(SPOR|SPORT|DAZN|SKY SPORT|BUNDESLIGA|PREMIER LEAGUE|MAGENTA|FUSSBALL|A SPOR|TRT SPOR|S SPORT|TIVIBU|TABII SPOR|BEIN SPORTS|NBA|EXXEN SPORTS|FB TV|EUROSPORT|SPORTS TV|SPOR SMART)\b/i,
  },
  {
    name: "ALMANYA",
    re: /\b(ARD|ZDF|RTL|PRO SIEBEN|PRO7|SAT\.1|SAT1|VOX|NITRO|KABEL 1|SUPER RTL|NICKELODEON|DMAX|HISTORY CHANNEL|NDR|KIKA|ALMANYA|DEUTSCH)\b/i,
  },
  {
    name: "Çocuk",
    re: /\b(ÇOCUK|COCUK|KINDER|KIDS|CARTOON|DISNEY|NICK|BABY|MINIKA|KIKA|TOGGO|TRT ÇOCUK)\b/i,
  },
  {
    name: "Film",
    re: /\b(SINEMA|CINEMA|MOVIE|FILM|YESILCAM|BOX OFFICE|FX|SHOWMAX|KINGBOX|ARENA BOX|BEIN MOVIES)\b/i,
  },
  {
    name: "Dini",
    re: /\b(DİYANET|AKIT|MEHTAP|HİLAL|KUDUS|SEMERKAND|MERCAN|VUSLAT|KARDELEN|DOST TV|YOL TV|TVNET|DINI|İSLAM)\b/i,
  }
];

// 🔥 Alle Kanäle werden behalten – Kategorien dienen nur zur Sortierung
function categorize(name) {
  const s = normalizeForCategory(name);
  for (const rule of CATEGORY_RULES) {
    if (rule.re.test(s)) return rule.name;
  }
  // Alle Kanäle, die in keine Kategorie passen, landen in "Sonstige"
  return "Sonstige";
}