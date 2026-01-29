GITHUB_URL = "https://github.com"
DIFF_APPENDIX = ".diff"
HTTP_OK = 200
BASE_PROMPT = "Please conduct a code review for the changes from %s...%s, with PR title: %s\\nPR description: %s\\n" \
              "**write the review comments to comments.txt(if the file exists, clear it first)**," \
              " following the format:\\n" \
              "<path>[file_path]</path>\\n" \
              "<side>left</side> or <side>right</side>\\n" \
              "<from>[begin_line_number]</from>\\n" \
              "<to>[end_line_number]</to>\\n" \
              "<note>[detailed_review_comment]</note>\\n" \
              "<notesplit />\\n" \
              "**If you think there are no issues with the code, there's no need to create the file or write comments**. " \
              "If possible, use code-reviewer first."
