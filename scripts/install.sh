cd "/Users/wendell/Desktop/tiktok_crawler"
source "/Users/wendell/miniconda3/etc/profile.d/conda.sh"
conda activate douyin-crawler

python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
