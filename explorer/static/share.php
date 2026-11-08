<?php
/*
 * The page for one game, e.g. https://games.example.com/game/178026  (the .htaccess maps that to
 * share.php?id=178026).
 *
 * It returns the whole app (index.html) with the preview tags for that game swapped in, so the same address
 * works for people (the app opens on that game) and for chat apps like Discord, which fetch the link, don't
 * run JavaScript, and read the "Open Graph" tags to draw a preview card.
 *
 * It only reads the game's small detail file (data/<version>/d/<n>.dat) that the build already writes.
 * build_static.py can also pre-build the same tags as tiny HTML pages for hosts without PHP (--no-php,
 * --share-pages); keep the two in step (the build's share_tags() mirrors describe() and tags() below, and
 * the tests compare them).
 */
declare(strict_types=1);

const SHARD = 500;                                  // games per detail file (build_static.py SHARD)
const IGDB_IMAGES = 'https://images.igdb.com/igdb/image/upload/';
const TAGS_BEGIN = '<!--SHARE_TAGS-->';
const TAGS_END = '<!--/SHARE_TAGS-->';

function h(string $s): string {
    return htmlspecialchars($s, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}

/* 12.5 -> "12.5 h", 0.4 -> "24 min" (same wording as the page) */
function hours_text(float $hours): string {
    if ($hours < 1) {
        return max(1, (int)floor($hours * 60 + 0.5)) . ' min';
    }
    if ($hours < 10) {
        return rtrim(rtrim(number_format(floor($hours * 10 + 0.5) / 10, 1, '.', ''), '0'), '.') . ' h';
    }
    return (int)floor($hours + 0.5) . ' h';
}

/* The first $max characters, cut back to a word boundary, with an ellipsis if anything was left out */
function excerpt(string $s, int $max = 240): string {
    $s = trim((string)preg_replace('/[ \t\r\n\f\v]+/', ' ', $s), " \t\r\n\f\v");   // ASCII whitespace only, to match the build's copy
    if (preg_match('/^.{0,' . $max . '}/us', $s, $m) !== 1 || strlen($m[0]) === strlen($s)) {
        return $s;
    }
    $cut = $m[0];
    $space = strrpos($cut, ' ');
    if ($space !== false && $space > $max / 2) {
        $cut = substr($cut, 0, $space);
    }
    return rtrim($cut, " ,;:.-") . '…';
}

function load_entry(int $id): ?array {
    $data = __DIR__ . '/data';
    $manifest = @file_get_contents($data . '/manifest.json');
    if ($manifest === false) {
        return null;
    }
    $version = (string)(json_decode($manifest, true)['version'] ?? '');
    if (preg_match('/^[0-9A-Za-z_-]+$/', $version) !== 1) {
        return null;
    }
    $raw = @file_get_contents($data . '/' . $version . '/d/' . intdiv($id, SHARD) . '.dat');
    if ($raw === false) {
        return null;
    }
    $json = (substr($raw, 0, 2) === "\x1f\x8b") ? @gzdecode($raw) : $raw;   // gzip'd JSON, like the browser reads it
    if ($json === false) {
        return null;
    }
    $shard = json_decode($json, true);
    $entry = is_array($shard) ? ($shard[(string)$id] ?? null) : null;
    return is_array($entry) ? $entry : null;
}

/* Title, description and image for a game's preview. */
function describe(array $g, int $id): array {
    $name = (string)($g['name'] ?? ('Game ' . $id));
    $title = $name . (isset($g['release']) ? ' (' . gmdate('Y', (int)$g['release']) . ')' : '');

    $facts = [];
    if (isset($g['rating'])) {
        $fact = '★ ' . number_format(floor(((float)$g['rating'] / 20) * 10 + 0.5) / 10, 1, '.', '');
        $votes = (int)($g['votes'] ?? 0);
        if ($votes > 0) {
            $fact .= ' (' . number_format($votes) . ($votes === 1 ? ' rating)' : ' ratings)');
        }
        $facts[] = $fact;
    }
    if (isset($g['time_to_beat']['normally'])) {
        $facts[] = hours_text((float)$g['time_to_beat']['normally'] / 3600) . ' to beat';
    }
    if (!empty($g['genres'])) {
        $facts[] = implode(', ', array_slice($g['genres'], 0, 3));
    }
    if (!empty($g['platforms'])) {
        $facts[] = implode(', ', array_slice($g['platforms'], 0, 4));
    }
    $description = implode(' · ', $facts);
    $summary = excerpt((string)($g['summary'] ?? ''));
    if ($summary !== '') {
        $description .= ($description !== '' ? "\n\n" : '') . $summary;
    }
    if ($description === '') {
        $description = 'A game in the VideoGameData database.';
    }

    // the cover makes a small preview card; a game with no cover falls back to its first (landscape) screenshot
    $image = null;
    $safe = fn($v) => is_string($v) && preg_match('/^[A-Za-z0-9_-]+$/', $v) === 1;
    if ($safe($g['cover'] ?? null)) {
        $image = [IGDB_IMAGES . 't_cover_big_2x/' . $g['cover'] . '.jpg', 528, 748, 'summary'];
    } elseif (!empty($g['screenshots']) && $safe($g['screenshots'][0])) {
        $image = [IGDB_IMAGES . 't_screenshot_huge/' . $g['screenshots'][0] . '.jpg', 1280, 720, 'summary_large_image'];
    }
    return [$title, $description, $image];
}

/* The tags that go in the page's <head>, one per line. */
function tags(string $title, string $description, ?array $image): string {
    $out  = '<title>' . h($title) . " · VideoGameData</title>\n";
    $out .= '<meta name="description" content="' . h($description) . "\">\n";
    $out .= "<meta property=\"og:site_name\" content=\"VideoGameData\">\n<meta property=\"og:type\" content=\"website\">\n";
    $out .= '<meta property="og:title" content="' . h($title) . "\">\n";
    $out .= '<meta property="og:description" content="' . h($description) . "\">\n";
    if ($image) {
        $out .= '<meta property="og:image" content="' . h($image[0]) . "\">\n";
        $out .= '<meta property="og:image:width" content="' . $image[1] . "\">\n";
        $out .= '<meta property="og:image:height" content="' . $image[2] . "\">\n";
    }
    $out .= '<meta name="twitter:card" content="' . ($image ? $image[3] : 'summary') . '">';
    return $out;
}

/* The app page with its preview tags replaced, or null if index.html isn't there / has no tag block. */
function app_page(string $tags): ?string {
    $index = @file_get_contents(__DIR__ . '/index.html');
    if ($index === false) {
        return null;
    }
    $begin = strpos($index, TAGS_BEGIN);
    $end = strpos($index, TAGS_END);
    if ($begin === false || $end === false || $end < $begin) {
        return null;
    }
    return substr($index, 0, $begin + strlen(TAGS_BEGIN)) . "\n" . $tags . "\n" . substr($index, $end);
}

/* ---- request ---- */
$id = (isset($_GET['id']) && ctype_digit((string)$_GET['id']) && strlen((string)$_GET['id']) < 10) ? (int)$_GET['id'] : 0;
$base = rtrim(str_replace('\\', '/', dirname($_SERVER['SCRIPT_NAME'] ?? '/')), '/');     // '' at the site root, '/games' under a path
$game = $id > 0 ? load_entry($id) : null;

header('Content-Type: text/html; charset=utf-8');
header('Cache-Control: no-cache');                  // index.html changes with every deploy
if ($game === null) {
    http_response_code(404);                        // the app still opens and says the game wasn't found
    $page = app_page(tags('Game not found', 'This game is not in the VideoGameData database.', null));
    echo $page ?? '<!doctype html><meta charset="utf-8"><p>Game not found. <a href="' . h($base . '/') . '">VideoGameData</a></p>';
    exit;
}
[$title, $description, $image] = describe($game, $id);
$tags = tags($title, $description, $image);
$page = app_page($tags);
if ($page === null) {                               // no index.html to wrap: a tiny page that sends people to the app
    $to = $base . '/#/game/' . $id;
    echo "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n" . $tags . "\n"
       . '<meta http-equiv="refresh" content="0; url=' . h($to) . "\">\n"
       . '<script>location.replace(' . json_encode($to, JSON_UNESCAPED_SLASHES) . ");</script>\n</head>\n"
       . '<body><p><a href="' . h($to) . '">' . h($title) . "</a> on VideoGameData</p></body>\n</html>\n";
    exit;
}
echo $page;
