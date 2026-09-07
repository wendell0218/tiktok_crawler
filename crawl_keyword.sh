cd "/Users/wendell/Desktop/tiktok_crawler"
source "/Users/wendell/miniconda3/etc/profile.d/conda.sh"
conda activate douyin-crawler

KEYWORDS="街舞,爵士舞,古典舞,民族舞,韩舞,拉丁舞,广场舞,芭蕾舞,现代舞,宅舞,曳步舞,舞蹈翻跳,原创编舞,舞蹈挑战,舞蹈"
WORKERS=1
COUNT=200
OUTPUT_DIR="downloads"
QUALITY="best"
SEARCH_DELAY=5
KEYWORD_DELAY=60

python crawl_keyword.py --keywords "$KEYWORDS" --workers "$WORKERS" --count "$COUNT" --output-dir "$OUTPUT_DIR" --quality "$QUALITY" --search-delay "$SEARCH_DELAY" --keyword-delay "$KEYWORD_DELAY"
