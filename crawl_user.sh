cd "/Users/wendell/Desktop/tiktok_crawler"
source "/Users/wendell/miniconda3/etc/profile.d/conda.sh"
conda activate douyin-crawler

TARGET_USER="影视飓风"
WORKERS=1
COUNT=0
OUTPUT_DIR="downloads/users"
QUALITY="best"
PAGE_DELAY=5

python crawl_user.py --user "$TARGET_USER" --workers "$WORKERS" --count "$COUNT" --output-dir "$OUTPUT_DIR" --quality "$QUALITY" --page-delay "$PAGE_DELAY"
