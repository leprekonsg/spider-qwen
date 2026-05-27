/* global React */

function Icon({ name, size = 18, stroke = 1.5, color = "currentColor" }) {
  const paths = {
    search:    <><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></>,
    suppliers: <><circle cx="12" cy="6" r="3"/><circle cx="5" cy="18" r="3"/><circle cx="19" cy="18" r="3"/><path d="M12 9v3M9 17 7 14M15 17l2-3"/></>,
    contracts: <><path d="M6 3h9l3 3v15H6Z"/><path d="M9 9h6M9 13h6M9 17h4"/></>,
    risks:     <><path d="M12 2 2 22h20Z"/><path d="M12 10v5M12 19h.01"/></>,
    insights:  <><path d="M3 12a9 9 0 1 0 9-9"/><path d="M3 12h9V3"/></>,
    rfq:       <><path d="M4 6h16M4 12h10M4 18h16"/><path d="m15 9 4 3-4 3"/></>,
    ledger:    <><path d="M4 4h12l4 4v12H4z"/><path d="M8 12h8M8 16h6"/></>,
    history:   <><path d="M3 12a9 9 0 1 0 3-7"/><path d="M3 5v5h5"/><path d="M12 8v4l3 2"/></>,
    settings:  <><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1A1.7 1.7 0 0 0 9 19.4a1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1A1.7 1.7 0 0 0 15 4.6a1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"/></>,
    spider:    <><circle cx="12" cy="13" r="3" fill="currentColor" stroke="none"/><path d="M12 10V6M9 12 5 9M15 12l4-3M9 14l-4 3M15 14l4 3M12 16v4"/></>,
    bell:      <><path d="M6 8a6 6 0 0 1 12 0v5l2 3H4l2-3Z"/><path d="M10 19a2 2 0 0 0 4 0"/></>,
    arrow:     <><path d="M5 12h14M13 6l6 6-6 6"/></>,
    plus:      <><path d="M12 5v14M5 12h14"/></>,
    check:     <><path d="m5 12 5 5 9-11"/></>,
    x:         <><path d="M6 6l12 12M18 6 6 18"/></>,
    filter:    <><path d="M3 5h18l-7 9v6l-4-2v-4Z"/></>,
    info:      <><circle cx="12" cy="12" r="9"/><path d="M12 8h.01M11 12h1v5h1"/></>,
    chevron:   <><path d="m9 6 6 6-6 6"/></>,
    chevronDown: <><path d="m6 9 6 6 6-6"/></>,
    spark:     <><path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6"/></>,
    globe:     <><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18 14 14 0 0 1 0-18"/></>,
    link:      <><path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 1 0-7-7l-1 1"/><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 1 0 7 7l1-1"/></>,
    shield:    <><path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6Z"/></>,
    eye:       <><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></>,
    file:      <><path d="M6 3h9l3 3v15H6Z"/><path d="M15 3v3h3"/></>,
    pause:     <><rect x="6" y="5" width="4" height="14"/><rect x="14" y="5" width="4" height="14"/></>,
    play:      <><path d="M6 4v16l14-8z"/></>,
    target:    <><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1" fill="currentColor"/></>,
    send:      <><path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4Z"/></>,
    refresh:   <><path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 4v5h-5"/></>,
    sparkle:   <><path d="M12 3l1.6 5.4L19 10l-5.4 1.6L12 17l-1.6-5.4L5 10l5.4-1.6Z"/></>,
    web:       <><circle cx="12" cy="12" r="9"/><path d="M12 3v18M3 12h18M5.6 5.6l12.8 12.8M5.6 18.4 18.4 5.6"/></>,
  };
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={stroke} strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
      {paths[name] || null}
    </svg>
  );
}

window.SQIcon = Icon;
