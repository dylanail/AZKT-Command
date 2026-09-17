/* A QR code drawn from the module grid the server returns (backend/app/auth/passkey.py `_qr`).
   Always dark-on-white, in both themes: a scanner needs the contrast and the quiet zone, not our tokens. */
export interface QrCodeProps {
  /** One string per row, "1" = dark module. Square. */
  rows: string[];
  /** Rendered size in px (the grid scales to it). */
  size?: number;
  label?: string;
}

/** Horizontal runs of dark modules as one path, so a 41×41 code is one element and not 1,681. */
function runs(rows: string[], quiet: number): string {
  const d: string[] = [];
  rows.forEach((row, y) => {
    let x = 0;
    while (x < row.length) {
      if (row[x] !== "1") { x += 1; continue; }
      let end = x;
      while (end + 1 < row.length && row[end + 1] === "1") end += 1;
      d.push(`M${x + quiet} ${y + quiet}h${end - x + 1}v1h-${end - x + 1}z`);
      x = end + 1;
    }
  });
  return d.join("");
}

export function QrCode({ rows, size = 208, label = "QR code" }: QrCodeProps) {
  if (!rows.length) return null;
  const quiet = 3;  // the mandatory quiet zone, or phones won't lock on
  const dim = rows.length + quiet * 2;
  return (
    <svg className="qr" viewBox={`0 0 ${dim} ${dim}`} width={size} height={size} role="img" aria-label={label} shapeRendering="crispEdges">
      <rect width={dim} height={dim} fill="#ffffff" />
      <path d={runs(rows, quiet)} fill="#000000" />
    </svg>
  );
}
