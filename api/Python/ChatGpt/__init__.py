import logging, json, os
import azure.functions as func
import openai
from langchain.embeddings.openai import OpenAIEmbeddings
import os
from langchain.llms import OpenAIChat
from datetime import datetime, timedelta
from azure.storage.blob import BlobServiceClient, generate_blob_sas, generate_container_sas
from langchain.vectorstores import Pinecone
import pinecone
from azure.storage.blob import BlobServiceClient
from langchain.embeddings.openai import OpenAIEmbeddings
from langchain.prompts import PromptTemplate
from langchain.embeddings.openai import OpenAIEmbeddings
import pinecone
from langchain.chains.qa_with_sources import load_qa_with_sources_chain
from langchain.chains import VectorDBQAWithSourcesChain
from langchain.llms.openai import OpenAI, AzureOpenAI
#from langchain.vectorstores.redis import Redis
from redis import Redis
from redis.commands.search.query import Query
from redis.commands.search.indexDefinition import IndexDefinition, IndexType
from redis.commands.search.field import VectorField, TagField, TextField
import numpy as np
from langchain.docstore.document import Document
import tiktoken
from typing import Mapping

OpenAiKey = os.environ['OpenAiKey']
OpenAiApiKey = os.environ['OpenAiApiKey']
OpenAiEndPoint = os.environ['OpenAiEndPoint']
OpenAiVersion = os.environ['OpenAiVersion']
OpenAiDavinci = os.environ['OpenAiDavinci']
OpenAiService = os.environ['OpenAiService']
OpenAiDocStorName = os.environ['OpenAiDocStorName']
OpenAiDocStorKey = os.environ['OpenAiDocStorKey']
OpenAiDocConnStr = f"DefaultEndpointsProtocol=https;AccountName={OpenAiDocStorName};AccountKey={OpenAiDocStorKey};EndpointSuffix=core.windows.net"
OpenAiDocContainer = os.environ['OpenAiDocContainer']
PineconeEnv = os.environ['PineconeEnv']
PineconeKey = os.environ['PineconeKey']
VsIndexName = os.environ['VsIndexName']
OpenAiChatDocStorName =  os.environ['OpenAiChatDocStorName']
OpenAiChatDocContainer =  os.environ['OpenAiChatDocContainer']
OpenAiChat = os.environ['OpenAiChat']
OpenAiChatBlobKey = os.environ['OpenAiChatBlobKey']
OpenAiEmbedding = os.environ['OpenAiEmbedding']
RedisAddress = os.environ['RedisAddress']
RedisPassword = os.environ['RedisPassword']
OpenAiEmbedding = os.environ['OpenAiEmbedding']
RedisPort = os.environ['RedisPort']

redisUrl = "redis://default:" + RedisPassword + "@" + RedisAddress + ":" + RedisPort
redisConnection = Redis(host= RedisAddress, port=RedisPort, password=RedisPassword) #api for Docker localhost for local execution

ChatBlobClient = BlobServiceClient(
    account_url=f"https://{OpenAiChatDocStorName}.blob.core.windows.net",
    credential=OpenAiChatBlobKey)
ChatBlobContainer = ChatBlobClient.get_container_client(OpenAiChatDocContainer)

def getEmbedding(text: str, engine="text-embedding-ada-002") -> list[float]:
    try:
        text = text.replace("\n", " ")
        EMBEDDING_ENCODING = 'cl100k_base' if engine == 'text-embedding-ada-002' else 'gpt2'
        encoding = tiktoken.get_encoding(EMBEDDING_ENCODING)
        return openai.Embedding.create(input=encoding.encode(text), engine=engine)["data"][0]["embedding"]
    except Exception as e:
        logging.info(e)

def performRedisSearch(question, indexName, k):
    #embeddingQuery= Redis.embedding_function(question)
    question = question.replace("\n", " ")
    embeddingQuery = getEmbedding(question, engine=OpenAiEmbedding)
    arrayEmbedding = np.array(embeddingQuery)
    returnField = ["metadata", "content", "vector_score"]
    vectorField = "content_vector"
    hybridField = "*"
    baseQuery = (
        f"{hybridField}=>[KNN {k} @{vectorField} $vector AS vector_score]"
    )
    redisQuery = (
        Query(baseQuery)
        .return_fields(*returnField)
        .sort_by("vector_score")
        .paging(0, 5)
        .dialect(2)
    )
    params_dict: Mapping[str, str] = {
            "vector": np.array(arrayEmbedding)  # type: ignore
            .astype(dtype=np.float32)
            .tobytes()
    }

    # perform vector search
    results = redisConnection.ft(indexName).search(redisQuery, params_dict)

    documents = [
        Document(page_content=result.content, metadata=json.loads(result.metadata))
        for result in results.docs
    ]

    return documents

def main(req: func.HttpRequest, context: func.Context) -> func.HttpResponse:
    logging.info(f'{context.function_name} HTTP trigger function processed a request.')
    if hasattr(context, 'retry_context'):
        logging.info(f'Current retry count: {context.retry_context.retry_count}')

        if context.retry_context.retry_count == context.retry_context.max_retry_count:
            logging.info(
                f"Max retries of {context.retry_context.max_retry_count} for "
                f"function {context.function_name} has been reached")

    try:
        indexNs = req.params.get('indexNs')
        indexType = req.params.get('indexType')
        body = json.dumps(req.get_json())
    except ValueError:
        return func.HttpResponse(
             "Invalid body",
             status_code=400
        )

    if body:
        pinecone.init(
            api_key=PineconeKey,  # find at app.pinecone.io
            environment=PineconeEnv  # next to api key in console
        )
        result = ComposeResponse(body, indexNs, indexType)
        return func.HttpResponse(result, mimetype="application/json")
    else:
        return func.HttpResponse(
             "Invalid body",
             status_code=400
        )

def ComposeResponse(jsonData, indexNs, indexType):
    values = json.loads(jsonData)['values']

    logging.info("Calling Compose Response")
    # Prepare the Output before the loop
    results = {}
    results["values"] = []

    for value in values:
        outputRecord = TransformValue(value, indexNs, indexType)
        if outputRecord != None:
            results["values"].append(outputRecord)
    return json.dumps(results, ensure_ascii=False)

def getChatHistory(history, includeLastTurn=True, maxTokens=1000) -> str:
    historyText = ""
    for h in reversed(history if includeLastTurn else history[:-1]):
        historyText = """<|im_start|>user""" +"\n" + h["user"] + "\n" + """<|im_end|>""" + "\n" + """<|im_start|>assistant""" + "\n" + (h.get("bot") + """<|im_end|>""" if h.get("bot") else "") + "\n" + historyText
        if len(historyText) > maxTokens*4:
            break
    return historyText

def GetRrrAnswer(history, indexNs, indexType):
    promptPrefix = """<|im_start|>system
    Be brief in your answers.
    Answer ONLY with the facts listed in the list of sources below. If there isn't enough information below, say you don't know. Do not generate answers that don't use the sources below. If asking a clarifying question to the user would help, ask the question.
    Each source has a name followed by colon and the actual information, always include the source name for each fact you use in the response. Use square brackets to reference the source, e.g. [info1.txt]. Don't combine sources, list each source separately, e.g. [info1.txt][info2.pdf].
    {follow_up_questions_prompt}
    Sources:
    {sources}
    <|im_end|>
    {chat_history}
    """

    followupQaPromptTemplate = """Generate three very brief follow-up questions that the user would likely ask next.
    Use double angle brackets to reference the questions, e.g. <<Is there a more details on that?>>.
    Try not to repeat questions that have already been asked.
    Only generate questions and do not generate any text before or after the questions, such as 'Next Questions'"""

    qaPromptTemplate = """Below is a history of the conversation so far, and a new question asked by the user that needs to be answered by searching in a knowledge base.
    Generate a search query based on the conversation and the new question.
    Do not include cited source filenames and document names e.g info.txt or doc.pdf in the search query terms.
    Do not include any text inside [] or <<>> in the search query terms.
    If the question is not in English, translate the question to English before generating the search query.

    Chat History:
    {chat_history}

    Question:
    {question}

    Search query:
    """

    openai.api_type = "azure"
    openai.api_key = OpenAiKey
    openai.api_version = OpenAiVersion
    openai.api_base = f"https://{OpenAiService}.openai.azure.com"

    # STEP 1: Generate an optimized keyword search query based on the chat history and the last question
    optimizedPrompt = qaPromptTemplate.format(chat_history=getChatHistory(history, includeLastTurn=False),
                                              question=history[-1]["user"])

    #logging.info("Optimized Prompt" + optimizedPrompt)

    completion = openai.Completion.create(
        engine=OpenAiDavinci,
        prompt=optimizedPrompt,
        temperature=0.0,
        max_tokens=32,
        n=1,
        stop=["\n"])
    q = completion.choices[0].text

    logging.info("Question " + completion.choices[0].text)

    # STEP 2: Retrieve relevant documents from the search index with the GPT optimized query
    embeddings = OpenAIEmbeddings(document_model_name=OpenAiEmbedding, chunk_size=1, openai_api_key=OpenAiKey)
    if indexType == 'pinecone':
        vectorDb = Pinecone.from_existing_index(index_name=VsIndexName, embedding=embeddings)
        logging.info("Pinecone Setup done to search against - " + indexNs)
        docs = vectorDb.similarity_search(q, k=5, namespace=indexNs)
        logging.info("Executed Index and found ")
    elif indexType == "redis":
        try:
            #vectorDb = Redis(redis_url=redisUrl, index_name=indexNs, embedding_function=embeddings)
            #logging.info("Redis Setup done")
            #docs = vectorDb.similarity_search(q, k=5, index_name=indexNs)
            docs = performRedisSearch(q, indexNs, 5)
        except:
            return {"data_points": "", "answer": "Working on fixing Redis Implementation", "thoughts": ""}
        
    elif indexType == 'milvus':
        docs = []

    rawDocs = []
    for doc in docs:
      rawDocs.append(doc.page_content)
    #content = "\n".join(docs)

    # Allow client to replace the entire prompt, or to inject into the exiting prompt using >>>
    finalPrompt = promptPrefix.format(injected_prompt="", sources=rawDocs,
                                      chat_history=getChatHistory(history),
                                      follow_up_questions_prompt=followupQaPromptTemplate)
    logging.info("Final Prompt created")
    # STEP 3: Generate a contextual and content specific answer using the search results and chat history
    try:
        completion = openai.Completion.create(
            engine=OpenAiChat,
            prompt=finalPrompt,
            temperature=0.7,
            max_tokens=1024,
            n=1,
            stop=["<|im_end|>", "<|im_start|>"])
    except Exception as e:
        logging.error(e)
        return {"data_points": rawDocs, "answer": "Working on fixing OpenAI Implementation - Error " + str(e) , "thoughts": ""}
    
    return {"data_points": rawDocs, "answer": completion.choices[0].text, "thoughts": f"Searched for:<br>{q}<br><br>Prompt:<br>" + finalPrompt.replace('\n', '<br>')}

    # llm = OpenAIChat(deployment_name=OpenAiChat,
    #           temperature=0.7,
    #           openai_api_key=OpenAiApiKey,
    #           max_tokens=1024,
    #           batch_size=10)
    # logging.info("LLM Setup done")
    # chainType = 'stuff'
    # followupPrompt = PromptTemplate(template=promptPrefix, input_variables=['sources', 'chat_history', 'follow_up_questions_prompt'])
    # qaChain = load_qa_with_sources_chain(llm, chain_type=chainType, prompt=followupPrompt)

    # # STEP 3: Generate a contextual and content specific answer using the search results and chat history
    # chain = VectorDBQAWithSourcesChain(combine_documents_chain=qaChain, vectorstore=vectorDb)
    # answer = chain({"question": q}, return_only_outputs=False)

    # return {"answer": answer, "thoughts": f"Searched for:<br>{q}<br><br>Prompt:<br>" + followupPrompt.replace('\n', '<br>')}

def GetAnswer(history, approach, overrides, indexNs, indexType):
    logging.info("Getting Answer")
    try:
      logging.info("Loading OpenAI")
      if (approach == 'rrr'):
        r = GetRrrAnswer(history, indexNs, indexType)
      else:
          return json.dumps({"error": "unknown approach"})
      return r
    except Exception as e:
      logging.error(e)
      return func.HttpResponse(
            "Error getting files",
            status_code=500
      )

def TransformValue(record, indexNs, indexType):
    logging.info("Calling Transform Value")
    try:
        recordId = record['recordId']
    except AssertionError  as error:
        return None

    # Validate the inputs
    try:
        assert ('data' in record), "'data' field is required."
        data = record['data']
        #assert ('text' in data), "'text' field is required in 'data' object."

    except KeyError as error:
        return (
            {
            "recordId": recordId,
            "errors": [ { "message": "KeyError:" + error.args[0] }   ]
            })
    except AssertionError as error:
        return (
            {
            "recordId": recordId,
            "errors": [ { "message": "AssertionError:" + error.args[0] }   ]
            })
    except SystemError as error:
        return (
            {
            "recordId": recordId,
            "errors": [ { "message": "SystemError:" + error.args[0] }   ]
            })

    try:
        # Getting the items from the values/data/text
        history = data['history']
        approach = data['approach']
        overrides = data['approach']

        summaryResponse = GetAnswer(history, approach, overrides, indexNs, indexType)
        return ({
            "recordId": recordId,
            "data": summaryResponse
            })

    except:
        return (
            {
            "recordId": recordId,
            "errors": [ { "message": "Could not complete operation for record." }   ]
            })