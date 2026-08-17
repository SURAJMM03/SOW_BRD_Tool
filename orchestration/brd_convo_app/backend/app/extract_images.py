import os
os.chdir(r"C:\24-04_BlueprintDocAgent\orchestration\brd_convo_app\backend\app")
 
from image_extractor import extract_and_index_images
 
# Use the correct project-specific path
result = extract_and_index_images("./uploads/6c951d0e/source")
 
print(f"✅ Total images extracted: {result['total_images']}")
print(f"   Output directory: {result['output_dir']}")
print(f"   Index file: {result['index_file']}")