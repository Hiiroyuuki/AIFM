# TODO list/Development progress
---
## GUI Parts:

> [frontend.py](frontend.py)

- [x] New AIFolder.
- [ ] AIFolder zone, prespect.
- [x] Different Icon of AIFolder.
- [ ] Display Tags, description of file in Preview zone.

## AI Function Parts:

> [mainFunctions.py](mainFunctions.py)  
> [agent.py](agent.py)  
> [models.py](models.py)  
> [config_loader.py](config_loader.py)

- [x] Auto tags, description generation.
- [x] LLM APIKEY connection.
- [ ] LLM operation.  
- [ ] Install path recommandation.  
- [ ] Content embeddings (image embeddings; text embeddings).
  
&#10060;Smaller LLM used to generate tags (optional LOCAL or API).

## Extract the Files from selected Folder
> [files_extractors.py](files_extractors.py)  

Extract user-interested item from folders.

- [x] Base on everything core, user now can get the media/document from the folder they choosed.
- [x] Outside LLM can read/add/modify the rule/code/re of filters, helping user to get the items they want.
- [ ] 1. Set up the enter point of files_extractors for LLM;  
      2. Setup system prompt;  
      3. Leave the user prompt port; Finish the test; 
- [ ] Connect ths module into [frontend.py](frontend.py)  

---
---

### About Classifier
> [folderClassifier](discard_projects\folderClassifier\classifier.py)

Discarded