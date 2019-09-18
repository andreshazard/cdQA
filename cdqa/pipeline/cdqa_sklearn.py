import joblib
import warnings

import pandas as pd
import numpy as np
import torch

from sklearn.base import BaseEstimator

from cdqa.retriever import TfidfRetriever, BM25Retriever
from cdqa.utils.converters import generate_squad_examples
from cdqa.reader import BertProcessor, BertQA

RETRIEVERS = {"bm25": BM25Retriever, "tfidf": TfidfRetriever}


class QAPipeline(BaseEstimator):
    """
    A scikit-learn implementation of the whole cdQA pipeline

    Parameters
    ----------
    metadata: pandas.DataFrame
        dataframe containing your corpus of documents metadata
        header should be of format: title, paragraphs.
    reader: str (path to .joblib) or .joblib object of an instance of BertQA (BERT model with sklearn wrapper), optional
    retrieve_by_doc: bool (default: True). If Retriever will rank by documents
        or by paragraphs.
    kwargs: kwargs for BertQA(), BertProcessor(), TfidfRetriever() and BM25Retriever
        Please check documentation for these classes


    Examples
    --------
    >>> from cdqa.pipeline import QAPipeline
    >>> qa_pipeline = QAPipeline(reader='bert_qa_squad_vCPU-sklearn.joblib')
    >>> qa_pipeline.fit_retriever(X=df)
    >>> prediction = qa_pipeline.predict(X='When BNP Paribas was created?')

    >>> from cdqa.pipeline import QAPipeline
    >>> qa_pipeline = QAPipeline()
    >>> qa_pipeline.fit_reader('train-v1.1.json')
    >>> qa_pipeline.fit_retriever(X=df)
    >>> prediction = qa_pipeline.predict(X='When BNP Paribas was created?')

    """

    def __init__(self, reader=None, retriever="bm25", retrieve_by_doc=False, **kwargs):

        if retriever not in RETRIEVERS:
            raise ValueError(
                "You provided a type of retriever that is not supported. "
                + "Please provide a retriver in the following list: "
                + str(list(RETRIEVERS.keys()))
            )

        retriever_class = RETRIEVERS[retriever]

        # Separating kwargs
        kwargs_bertqa = {
            key: value
            for key, value in kwargs.items()
            if key in BertQA.__init__.__code__.co_varnames
        }

        kwargs_processor = {
            key: value
            for key, value in kwargs.items()
            if key in BertProcessor.__init__.__code__.co_varnames
        }

        kwargs_retriever = {
            key: value
            for key, value in kwargs.items()
            if key in retriever_class.__init__.__code__.co_varnames
        }

        if not reader:
            self.reader = BertQA(**kwargs_bertqa)
        elif type(reader) == str:
            self.reader = joblib.load(reader)
        else:
            self.reader = reader

        self.processor_train = BertProcessor(is_training=True, **kwargs_processor)

        self.processor_predict = BertProcessor(is_training=False, **kwargs_processor)

        self.retriever = retriever_class(**kwargs_retriever)

        self.retrieve_by_doc = retrieve_by_doc

    def fit_retriever(self, X=None, y=None):
        """ Fit the QAPipeline retriever to a list of documents in a dataframe.
         Parameters
        ----------
        X: pandas.Dataframe
            Dataframe with the following columns: "title", "paragraphs"
        """

        if self.retrieve_by_doc:
            self.metadata = X
            self.metadata["content"] = self.metadata["paragraphs"].apply(
                lambda x: " ".join(x)
            )
        else:
            self.metadata = self._expand_paragraphs(X)

        self.retriever.fit(self.metadata["content"])

        return self

    def fit_reader(self, X=None, y=None):
        """ Fit the QAPipeline retriever to a list of documents in a dataframe.

        Parameters
        ----------
        X: pandas.Dataframe
            Dataframe with the following columns: "title", "paragraphs"

        """

        train_examples, train_features = self.processor_train.fit_transform(X)
        self.reader.fit(X=(train_examples, train_features))

        return self

    def predict(self, X=None, return_logit=False, n_predictions=None):
        """ Compute prediction of an answer to a question

        Parameters
        ----------
        X: str or list of strings
            Sample (question) or list of samples to perform a prediction on

        return_logit: boolean
            Whether to return logit of best answer or not. Default: False

        Returns
        -------
        If X is str
        prediction: tuple (answer, title, paragraph)

        If X is list os strings
        predictions: list of tuples (answer, title, paragraph)

        If return_logits is True, each prediction tuple will have the following
        structure: (answer, title, paragraph, best logit)

        """
        if isinstance(X, str):
            closest_docs_indices = self.retriever.predict(X, metadata=self.metadata)
            squad_examples = generate_squad_examples(
                question=X,
                closest_docs_indices=closest_docs_indices,
                metadata=self.metadata,
                retrieve_by_doc=self.retrieve_by_doc,
            )
            examples, features = self.processor_predict.fit_transform(X=squad_examples)
            prediction = self.reader.predict(
                (examples, features), return_logit, n_predictions
            )
            return prediction

        elif isinstance(X, list):
            predictions = []
            for query in X:
                closest_docs_indices = self.retriever.predict(
                    query, metadata=self.metadata
                )
                squad_examples = generate_squad_examples(
                    question=query,
                    closest_docs_indices=closest_docs_indices,
                    metadata=self.metadata,
                )
                examples, features = self.processor_predict.fit_transform(
                    X=squad_examples
                )
                pred = self.reader.predict(
                    (examples, features), return_logit, n_predictions
                )
                predictions.append(pred)

            return predictions

        else:
            raise TypeError(
                "The input is not a string or a list. \
                            Please provide a string or a list of strings as input"
            )

    def to(self, device):
        """ Send reader to CPU if device=='cpu' or to GPU if device=='cuda'
        """
        if device not in ("cpu", "cuda"):
            raise ValueError("Attribute device should be 'cpu' or 'cuda'.")

        self.reader.model.to(device)
        self.reader.device = torch.device(device)
        return self

    def cpu(self):
        """ Send reader to CPU
        """
        self.reader.model.cpu()
        self.reader.device = torch.device("cpu")
        return self

    def cuda(self):
        """ Send reader to GPU
        """
        self.reader.model.cuda()
        self.reader.device = torch.device("cuda")
        return self

    def dump_reader(self, filename):
        """ Dump reader model to a .joblib object
        """
        joblib.dump(self.reader, filename)

    @staticmethod
    def _expand_paragraphs(df):
        # Snippet taken from: https://stackoverflow.com/a/48532692/11514226
        lst_col = "paragraphs"
        df = pd.DataFrame(
            {
                col: np.repeat(df[col].values, df[lst_col].str.len())
                for col in df.columns.drop(lst_col)
            }
        ).assign(**{lst_col: np.concatenate(df[lst_col].values)})[df.columns]
        df["content"] = df["paragraphs"]
        return df.drop("paragraphs", axis=1)
