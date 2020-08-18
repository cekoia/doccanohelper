/from sklearn.neighbors import LocalOutlierFactor
from sklearn.feature_extraction.text import HashingVectorizer
from sklearn.preprocessing import LabelEncoder
import os
import spacy
import srsly
import jsonlines
import pandas as pd, sklearn
from sklearn.model_selection import train_test_split
import re
import numpy as np
import spacy
import srsly
import logging
from spacy.gold import docs_to_json, biluo_tags_from_offsets, spans_from_biluo_tags
import shutil
from azure.storage.blob import BlobServiceClient
import logging
import en_core_web_sm
from doccano_api_client import DoccanoClient

def uploadtoazure(localpath, remotedir, connect_str):
  """
  upload un fichier vers azure
  @localpath: chemin du fichier en local
  @remotedir: répertoire de copie distant
  @connect_str: chaîne de connexion au bucket
  """
  localfilename=localpath.split('/')[-1]
  splits=remotedir.split("/")
  containername=splits[0]
  remotepath='/'.join(splits[1:])+'/'+localfilename
  logging.info(f'uploading {localpath} to {remotepath}')
  blob_service_client = BlobServiceClient.from_connection_string(connect_str)
  blob_client = blob_service_client.get_blob_client(container=containername, blob=remotepath)
  with open(localpath, "rb") as data:
      blob_client.upload_blob(data,overwrite=True)

def fileexist(remotepath,connect_str):
    """
    teste si un fichier cloud existe
    """
    splits=remotepath.split("/")
    containername=splits[0]
    client = BlobServiceClient.from_connection_string(connect_str)
    container_client=client.get_container_client(containername)
    pattern='/'.join(splits[1:])
    bc=list(container_client.list_blobs(name_starts_with=pattern))
    return len(bc)>0

def findprojectidbycustomer(doccano_client, customer):
        logging.info('searching doccano project for customer '+customer)
        #on recherche le projet s'il est présent
        for project in doccano_client.get_project_list().json():
            if project['name']==customer:
                logging.info('existing doccano project found for customer '+customer)
                return project['id']
        #s'il ne l'est pas, on le crée
        logging.info('creating doccano project for customer '+customer)
        data={'collaborative_annotation': False,'description': 'à compléter','guideline': 'à compléter','name': customer,'project_type': 'SequenceLabeling','randomize_document_order': False,'resourcetype':'SequenceLabelingProject'}
        project=doccano_client.post('v1/projects',data=data).json()
        logging.info('doccano project created successfully for customer '+customer)
        return project['id']

def createjsonlfilefromannotations(df):
  '''crée le fichier local d'annotations'''
  localannotationpath='annotation.jsonl'
  with jsonlines.open(localannotationpath, mode='w') as writer:
    for doctext,group in df.groupby(['docid','doctext']):
      labels=[[row['start'],row['end'],row['label']] for _,row in group.iterrows()]
      writer.write({'text':doctext[1],'labels':labels})
  return localannotationpath

def getdoccanoclient(resource):
    return DoccanoClient(
      f'https://cekoia{resource}doccano.azurewebsites.net/',
      'admin',
      'manager'
    )

def findallprojects(resource):
    doccano_client = getdoccanoclient(resource)
    projects=doccano_client.get_project_list().json()
    return [project['name'] for project in projects]

def exportdoccanoannotations(resource,customer,df):
  '''exporte les annotations vers doccano'''
  for c in ['start','end']:
    df[c]=df[c].astype(int)
  localannotationpath=createjsonlfilefromannotations(df)
  
  doccano_client = getdoccanoclient(resource)
  #on cherche l'id projet
  projectid=findprojectidbycustomer(doccano_client, customer)

  #on sauvegarde les raccourcis d'annotations
  labels=doccano_client.get_label_list(projectid).json()
  #on le supprime et on le recrée
  doccano_client.delete('v1/projects/{project_id}'.format(project_id=projectid))
  projectid=findprojectidbycustomer(doccano_client, customer)
  #on envoie les nouvelles annotations
  response=doccano_client.post_doc_upload(projectid, 'json',localannotationpath)

  #on recharge les raccourcis
  labelsbad=doccano_client.get_label_list(projectid).json()
  m=pd.DataFrame(labelsbad).merge(pd.DataFrame(labels),on='text')
  m=m[['id_x','text','prefix_key_y','suffix_key_y','background_color_y','text_color_y']]
  m.columns=['id','text','prefix_key','suffix_key','background_color','text_color']
  for _,data in m.iterrows():
    id=data["id"]
    doccano_client.patch(f'/v1/projects/{projectid}/labels/{id}',data=dict(data))
        
def importdoccanoannotations(resource,customer):
  '''importe les annotations depuis doccano, crée un fichier local d'annotations et renvoie les annotations en dataframes'''
  doccano_client = DoccanoClient(
      f'https://cekoia{resource}doccano.azurewebsites.net/',
      'admin',
      'manager'
  )
  projectid=findprojectidbycustomer(doccano_client, customer)
  if projectid==None:
    logging.error('projet doccano non trouvé')
    
  first=doccano_client.get_document_list(projectid).json()
  count=first['count']
  docs=doccano_client.exp_get_doc_list(projectid,count,0).json()['results']
  for doc in docs:
    for annotation in doc['annotations']:
        annotation['initialtext']=doc['text']
    
  labels=doccano_client.get_label_list(projectid).json()
  label2dict={}
  for label in labels:
    label2dict[label['id']]=label['text']
  labels=pd.DataFrame(labels)
  labels=labels[['id','text']]
  df=pd.concat([pd.DataFrame(doc['annotations']) for doc in docs])
  df=df.drop(['id','prob','user'],1)
  df=df.merge(labels, left_on='label',right_on='id').drop(['label','id'],1)
  df=df.rename(columns={'start_offset':'start','end_offset':'end','document':'docid','initialtext':'doctext','text':'label'})
  df['text']=df.apply(lambda l: l['doctext'][l['start']:l['end']],1)
  emptydocs=pd.DataFrame([{'docid':d['id'],'doctext':d['text']} for d in docs if d['annotations'] ==[]])
  df=pd.concat([df,emptydocs])
  #on recalcule les identifiants de documents
  df=df.sort_values(by='docid')
  groups=[]
  i=1
  for docid,group in df.groupby('docid'):
    group['docid']=i
    i+=1
    groups.append(group)
  df=pd.concat(group)

  localannotationpath=createjsonlfilefromannotations(df)
  return df,localannotationpath

def findcandidates(text, doctext):
  '''recherche les occurence d'un texte dans un document et renvoie leurs positions'''
  candidates=[]
  start=0
  while doctext.find(text,start)>-1:
    start=doctext.find(text,start)
    end=start+len(text)
    candidates.append({'text':text,'start':start,'end':end})
    start=end
  return pd.DataFrame(candidates)

def annotatefixedlabels(df,trainingids=2):
  '''repère les labels fixes dans les deux premiers documents et cherche à les créer s'ils sont présents dans les autres mais pas encore annotés
      @df: annotations actuelles
      @trainindids: nombre de documents à utiliser pour l'entraînement
      returns: la liste des annotations créées
  '''
  #on repère les labels fixes
  labelsfixes=df.query(f'docid<={trainingids}').groupby(['label','text']).docid.count().reset_index()
  labelsfixes=labelsfixes[(~labelsfixes.label.str.contains(" ")) & (labelsfixes.docid==2)]
  positionslabelsfixes=df.query(f'docid<={trainingids}').groupby(['label','text'])['start','end'].mean().reset_index()
  labelsfixes=labelsfixes.merge(positionslabelsfixes).drop('docid',1)
  
  results=[]
  #on scanne chaque annotation fixe pour chaque document de test
  for docid,group in df.query(f'docid>{trainingids}').groupby('docid'):
    for _,row in labelsfixes.iterrows():
      label=row['label']
      start=row['start']
      #si l'annotation est absente, on la cherche
      if (label is None) or (label not in group.label.values):
        text=row['text']
        doctext=group.iloc[0,:].doctext
        #on recherche des chaînes candidates
        candidates=findcandidates(text,doctext)
        #si on en a trouvé plusieurs, on garde la plus proche en position 
        if len(candidates)>0:
          candidates['targetstart']=start
          candidates['distance']=np.abs(candidates.start-candidates.targetstart)
          candidates=candidates.nsmallest(1, 'distance').drop(['targetstart','distance'],1)
          candidates['docid']=docid
          candidates['label']=label
          candidates['doctext']=doctext
          logging.info(f'missing fixed annotation {label} from doc {docid} found')
          results.append(candidates)
  if len(results)==0:
    logging.info('missing fixed annotations: None')
    return df
  else:
    return pd.concat([df.dropna(),pd.concat(results)])

def findanomalies(df):
  '''recherche les champs non remplis dans les documents'''
  anomalies=df.groupby(['label','docid']).start.count().reset_index().pivot_table(index='docid',columns='label')
  anomalies=anomalies.fillna(0)#.melt()
  anomalies.columns=anomalies.columns.get_level_values(1)
  anomalies=anomalies.reset_index().melt(id_vars=['docid'])
  return anomalies.query('value==0')

def findwhattocomplete(anomalies):
  '''constitue la liste des documents à remplir et les champs à trouver'''
  tocomplete=[]
  for label,group in anomalies.groupby('label'):
    docidstocomplete=list(group.docid.unique())
    tocomplete.append({'docids':docidstocomplete,'label':label})
  tocomplete=pd.DataFrame(tocomplete)
  tocomplete['docids']=tocomplete['docids'].astype(str)
  tocomplete=tocomplete.groupby('docids').label.apply(list).reset_index()
  tocomplete['docids']=tocomplete['docids'].tolist()
  return tocomplete

def autocompletedocs(df,localannotationpath):
  localdir='.'
  predictions=[]
  anomalies=findanomalies(df)
  tocomplete=findwhattocomplete(anomalies)

  for _,row in tocomplete.iterrows():
    targetdocids=eval(row['docids'])
    targetlabels=row['label']
    logging.info(f'autocompleting labels {targetlabels} for docids {targetdocids}')
    nlp = en_core_web_sm.load()
    docs,targetdocs = [],[]
    with jsonlines.open(localannotationpath) as reader:
      i=1
      for obj in reader:
          doc = nlp(obj.get('text'))
          labels=obj.get('labels')
          fixedlabels=[label for label in labels if label[2] in targetlabels]
          tags = biluo_tags_from_offsets(doc, fixedlabels)
          entities = spans_from_biluo_tags(doc, tags)
          doc.ents = entities
          if i not in targetdocids:
            docs.append(doc)
          i+=1
    train, test = train_test_split(docs, test_size=0.3, random_state=42)
    srsly.write_json(localdir+"/train.json", [docs_to_json(train)])
    srsly.write_json(localdir+"/test.json", [docs_to_json(test)])
    outputdir=localdir+'/outputs'
    try:
        os.remove(outputdir)
    except:
        logging('outputdir not removed')
    #on entraîne le modèle
    spacy.cli.train('en',outputdir,train_path=localdir+"/train.json",dev_path=localdir+"/test.json",pipeline='ner', n_iter=40, n_early_stopping=2,verbose=0)

    modelpath=localdir+'/outputs/model-best'#final
    nlp = spacy.blank('en')
    ner = nlp.create_pipe('ner')
    nlp.add_pipe(ner)
    nlp = nlp.from_disk(modelpath)
    test=df[df.docid.isin(targetdocids)][['docid','doctext']].drop_duplicates()
    
    for _,row in test.iterrows():
      prediction=pd.DataFrame([{'text':ent.text, 'label':ent.label_,'start':ent.start_char,'end':ent.end_char} for ent in nlp(row['doctext']).ents])
      if len(prediction)>0:
        prediction['docid']=row['docid']
        prediction=pd.concat([prediction[prediction.label.str.contains(' ')], prediction[~prediction.label.str.contains(' ')].drop_duplicates(subset=['label'])])
        predictions.append(prediction)
  predictions=pd.concat(predictions)
  
  #suppression des annotations se chevauchant
  predictionssanschevauchement=[]
  for _,row in predictions.iterrows():
    if len(df.query(f'docid=={row["docid"]} and ((start<={row["end"]} and {row["end"]}<=end) or (start<={row["start"]} and {row["start"]}<=end))'))==0:
      predictionssanschevauchement.append(row)
      logging.info(f'Autocompleting {row["label"]} for doc {row["docid"]}')
    else:
      logging.info(f'Not autocompleting {row["label"]} for doc {row["docid"]} because existing anotation found')
  docidtexts=df[['docid','doctext']].drop_duplicates()
  predictionssanschevauchement=pd.DataFrame(predictionssanschevauchement)
  display(predictionssanschevauchement)
  return pd.concat([df,predictionssanschevauchement.merge(docidtexts)]).reset_index(drop=True)

def findlocaloutliers(df):
  '''recherche pour chaque label les valeurs anormales'''
  df=df.dropna().reset_index(drop=True)
  
  #on vectorize les textes en remplaçant au préalable les chiffres par le caractère \d
  vectorizer = HashingVectorizer(n_features=10)
  formattedtext=df.text.astype(str).str.replace('\d+','0')
  patterns=vectorizer.fit_transform(formattedtext).toarray()
  dfa=pd.concat([df,pd.DataFrame(patterns)],1)
  
  #on parcourt chaque label en exécutant une recherche locale d'anomalies
  a=[]
  for label,group in dfa.groupby('label'):
    lof = LocalOutlierFactor()
    group.loc[:,'anomalies']=lof.fit_predict(group.drop(['docid','text','doctext','label','start','end'],1)).copy()
    a.append(group)
  a=pd.concat(a)
  a=a[a.anomalies==-1]
  a=a[['docid','start','end','label','text']]

  #on calcule les valeurs habituellement données au label
  texts=df.groupby(['label','text']).docid.count().reset_index()
  mostcommontextsbylabel=texts.groupby(['label']).docid.max().reset_index().merge(texts).drop_duplicates(subset='label').drop('docid',1)
  mostcommontextsbylabel=mostcommontextsbylabel.rename(columns={'text':'most common value'})
  #on renvoie les anomalies trouvées adossées aux valeurs habituellement données
  return a.merge(mostcommontextsbylabel)

def report(df):
  '''recherche les annotations manquantes ou dont le nombre d'occurences diffère des autres documents'''
  emptydocs=[]
  for label in df.dropna().label.unique():
    for docid in df[df.label.isna()].docid.unique():
      emptydocs.append({'label':label,'docid':docid,'start':0})
  emptydocs=pd.DataFrame(emptydocs)
  annotationsbydocs=df.groupby(['label','docid']).start.count().reset_index()
  annotationsbydocs=pd.concat([emptydocs,annotationsbydocs])
  annotationsbydocs=annotationsbydocs.pivot_table(index='docid',columns='label').fillna(0)
  
  controls=[]
  for c in annotationsbydocs.columns:
    #recherche d'annotations manquantes
    emptyannotation=annotationsbydocs[c]==0
    docswithemptyannotation=emptyannotation[emptyannotation==True].index.tolist()
    if len(docswithemptyannotation)>0:
      controls.append({'label':c[1],'docids':docswithemptyannotation,'problem':'missing'})
      
    #recherche d'annotations faites trop souvent
    fullfilledannotation=annotationsbydocs[c].copy()
    fullfilledannotation=fullfilledannotation[fullfilledannotation>0]
    lof = LocalOutlierFactor()
    anomalies=lof.fit_predict(fullfilledannotation.values.reshape(-1,1))
    anomalies=pd.DataFrame(anomalies, columns=['anomaly'],index=fullfilledannotation.index)
    anomalies=anomalies[anomalies.anomaly==-1]
    if len(anomalies)>0:
      controls.append({'label':c[1],'docids':anomalies.index.tolist(),'problem':'strange number of occurences'})
  controls=pd.DataFrame(controls)
  return controls

def train(localannotationpath,connect_str,customer,localdir='/tmp'):
  #on le parse pour créer les fichiers de modélisation
  nlp = en_core_web_sm.load()

  docs = []
  with jsonlines.open(localannotationpath) as reader:
    for obj in reader:
        doc = nlp(obj.get('text'))
        tags = biluo_tags_from_offsets(doc, obj.get('labels'))
        entities = spans_from_biluo_tags(doc, tags)
        doc.ents = entities
        docs.append(doc)
  train, test = train_test_split(docs, test_size=0.3, random_state=42)
  srsly.write_json(localdir+"/train.json", [docs_to_json(train)])
  srsly.write_json(localdir+"/test.json", [docs_to_json(test)])

  #on entraîne le modèle
  spacy.cli.train('en',localdir+'/outputs',train_path=localdir+"/train.json",dev_path=localdir+"/test.json",pipeline='ner', n_iter=100, n_early_stopping=5)

  #on le compresse et on l'uploade dans azure
  modelpath=localdir+'/outputs/model-best'
  zippath=localdir+'/model'
  shutil.make_archive(zippath, 'zip', modelpath)
  uploadtoazure(zippath+'.zip', f'customers/{customer}',connect_str)