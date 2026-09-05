cd "/Users/wendell/Desktop/tiktok_crawler"
source "/Users/wendell/miniconda3/etc/profile.d/conda.sh"
conda activate douyin-crawler

PYTHONPATH="/Users/wendell/Desktop/tiktok_crawler/src"
export PYTHONPATH

python -m dycrawler download \
  --database "/Users/wendell/Desktop/tiktok_crawler/data/douyin.db" \
  --output "/Users/wendell/Desktop/tiktok_crawler/downloads" \
  --quality best \
  --codec any \
  --limit 20 \
  --delay 2 \
  --delete-after
