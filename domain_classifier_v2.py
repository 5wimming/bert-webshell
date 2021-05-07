# -*- coding: utf-8 -*-

'''
@Author  :   Xu

@Software:   PyCharm

@File    :   domain_classifier.py

@Time    :   2019-11-06 14:25

@Desc    :  基于bert的分类模型的fine-tune的领域分类模型，模型准确率验证通过，但是需要修改模型的初始化方法

'''

import os, csv, random, collections, pickle
import tensorflow as tf
import numpy as np
import pickle as pkl
import pathlib

from queue import Queue
from threading import Thread
import modeling
import optimization
import tokenization
from config import Config


os.environ['CUDA_VISIBLE_DEVICES'] = '1'

basedir = str(pathlib.Path(os.path.abspath(__file__)).parent)

cf = Config()


class InputExample(object):
    """A single training/test example for simple sequence classification."""

    def __init__(self, guid, text_a, text_b=None, label=None):
        """Constructs a InputExample.

        Args:
          guid: Unique id for the example.
          text_a: string. The untokenized text of the first sequence. For single
            sequence tasks, only this sequence must be specified.
          text_b: (Optional) string. The untokenized text of the second sequence.
            Only must be specified for sequence pair tasks.
          label: (Optional) string. The label of the example. This should be
            specified for train and dev examples, but not for test examples.
        """
        self.guid = guid
        self.text_a = text_a
        self.text_b = text_b
        self.label = label

class InputFeatures(object):
    """A single set of features of data."""

    def __init__(self,
                 input_ids, input_mask, segment_ids, label_id, is_real_example=True):
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids
        self.label_id = label_id
        self.is_real_example = is_real_example

class DataProcessor(object):
    """Base class for data converters for sequence classification data sets."""

    def get_train_examples(self, data_dir):
        """Gets a collection of `InputExample`s for the train set."""
        raise NotImplementedError()

    def get_dev_examples(self, data_dir):
        """Gets a collection of `InputExample`s for the dev set."""
        raise NotImplementedError()

    def get_test_examples(self, data_dir):
        """Gets a collection of `InputExample`s for prediction."""
        raise NotImplementedError()

    def get_labels(self):
        """Gets the list of labels for this data set."""
        raise NotImplementedError()

    @classmethod
    def _read_tsv(cls, input_file, quotechar=None):
        """Reads a tab separated value file."""
        with tf.gfile.Open(input_file, "r") as f:
            reader = csv.reader(f, delimiter="\t", quotechar=quotechar)
            lines = []
            for line in reader:
                lines.append(line)
            return lines

class DomainProcessor(DataProcessor):
    """Processor for the FenLei data set (GLUE version)."""

    def get_train_examples(self, data_dir):
        file_path = os.path.join(data_dir, 'train.txt')
        with open(file_path, 'r', encoding="utf-8") as f:
            reader = f.readlines()
        random.seed(0)
        random.shuffle(reader)  # 注意要shuffle

        examples, self.labels = [], []
        for index, line in enumerate(reader):
            guid = 'train-%d' % index
            split_line = line.strip().split("\t")
            text_a = tokenization.convert_to_unicode(split_line[1])
            text_b = None
            label = split_line[0]
            examples.append(InputExample(guid=guid, text_a=text_a,
                                         text_b=text_b, label=label))
            self.labels.append(label)
        return examples

    def get_dev_examples(self, data_dir):
        file_path = os.path.join(data_dir, 'val.txt')
        with open(file_path, 'r', encoding="utf-8") as f:
            reader = f.readlines()
        random.shuffle(reader)

        examples = []
        for index, line in enumerate(reader):
            guid = 'dev-%d' % index
            split_line = line.strip().split('\t')
            text_a = tokenization.convert_to_unicode(split_line[1])
            text_b = None
            label = split_line[0]
            examples.append(InputExample(guid=guid, text_a=text_a,
                                         text_b=text_b, label=label))
        return examples

    def get_test_examples(self, data_dir):
        file_path = os.path.join(data_dir, 'cnews.test.txt')
        with open(file_path, 'r', encoding="utf-8") as f:
            reader = f.readlines()
        # random.shuffle(reader)  # 测试集不打乱数据，便于比较

        examples = []
        for index, line in enumerate(reader):
            guid = 'test-%d' % index
            split_line = line.strip().split("\t")
            text_a = tokenization.convert_to_unicode(split_line[1])
            text_b = None
            label = split_line[0]
            examples.append(InputExample(guid=guid, text_a=text_a,
                                         text_b=text_b, label=label))
        return examples

    def get_sentence_examples(self, questions):
        for index, data in enumerate(questions):
            guid = 'test-%d' % index
            text_a = tokenization.convert_to_unicode(str(data))
            text_b = None
            # label = str(0)
            label = self.labels[0]
            yield InputExample(guid=guid, text_a=text_a, text_b=text_b, label=label)

    def one_example(self, sentence):
        guid, label = 'pred-0', self.labels[0]
        text_a, text_b = sentence, None
        return InputExample(guid=guid, text_a=text_a, text_b=text_b, label=label)

    def get_labels(self):
        return sorted(set(self.labels), key=self.labels.index)  # 使用有序列表而不是集合。保证了标签正确

class DomainCLS():

    def __init__(self, batch_size=cf.batch_size):

        self.mode = None
        self.max_seq_length = cf.max_seq_length
        self.tokenizer = tokenization.FullTokenizer(vocab_file=cf.vocab_file, do_lower_case=True)
        self.batch_size = batch_size
        self.estimator = None
        self.processor = DomainProcessor()    # 加载训练、测试数据class
        tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.INFO)

    def set_mode(self, mode):
        self.mode = mode
        self.estimator = self.get_estimator()
        if mode == tf.estimator.ModeKeys.PREDICT:
            self.input_queue = Queue(maxsize=1)
            self.output_queue = Queue(maxsize=1)
            self.predict_thread = Thread(target=self.predict_from_queue, daemon=True)    #daemon守护进程
            self.predict_thread.start()

    def create_model(bert_config, is_training, input_ids, input_mask, segment_ids, labels, num_labels, use_one_hot_embeddings):
        """
        构建分类模型
        :param bert_config:
        :param is_training:
        :param input_ids:
        :param input_mask:
        :param segment_ids:
        :param labels:
        :param num_labels:
        :return:
        """
        model = modeling.BertModel(
            config=bert_config,
            is_training=is_training,
            input_ids=input_ids,
            input_mask=input_mask,
            token_type_ids=segment_ids,
            use_one_hot_embeddings=use_one_hot_embeddings)

        # In the demo, we are doing a simple classification task on the entire segment.
        #
        # If you want to use the token-level output, use model.get_sequence_output() instead.
        # embedding_layer = model.get_sequence_output()
        output_layer = model.get_pooled_output()

        hidden_size = output_layer.shape[-1].value

        output_weights = tf.get_variable(
            "output_weights", [num_labels, hidden_size],
            initializer=tf.truncated_normal_initializer(stddev=0.02))

        output_bias = tf.get_variable(
            "output_bias", [num_labels], initializer=tf.zeros_initializer())

        with tf.variable_scope("loss"):
            if is_training:
                # I.e., 0.1 dropout
                output_layer = tf.nn.dropout(output_layer, keep_prob=0.9)

            logits = tf.matmul(output_layer, output_weights, transpose_b=True)
            logits = tf.nn.bias_add(logits, output_bias)
            probabilities = tf.nn.softmax(logits, axis=-1)

            # 这里对分类样本进行加权操作，处理分类样本不均衡问题
            log_probs = tf.nn.log_softmax(logits, axis=-1)

            one_hot_labels = tf.one_hot(labels, depth=num_labels, dtype=tf.float32)

            per_example_loss = -tf.reduce_sum(one_hot_labels * log_probs, axis=-1)
            loss = tf.reduce_mean(per_example_loss)

            return (loss, per_example_loss, logits, probabilities)

    def model_fn_builder(self, bert_config, num_labels, init_checkpoint, learning_rate, num_train_steps, num_warmup_steps, use_one_hot_embeddings):
        """Returns `model_fn` closure for GPU Estimator."""

        def model_gpu(features, labels, mode, params):  # pylint: disable=unused-argument
            """The `model_fn` for GPU 版本的 Estimator."""

            tf.logging.info("*** Features ***")
            for name in sorted(features.keys()):
                tf.compat.v1.logging.info("  name = %s, shape = %s" % (name, features[name].shape))

            input_ids = features["input_ids"]
            input_mask = features["input_mask"]
            segment_ids = features["segment_ids"]
            label_ids = features["label_ids"]

            is_training = (mode == tf.estimator.ModeKeys.TRAIN)

            (total_loss, per_example_loss, logits, probabilities) = DomainCLS.create_model(
                bert_config, is_training, input_ids, input_mask, segment_ids, label_ids, num_labels, use_one_hot_embeddings)

            tvars = tf.compat.v1.trainable_variables()
            initialized_variable_names = {}

            if init_checkpoint:
                (assignment_map, initialized_variable_names) = modeling.get_assignment_map_from_checkpoint(tvars, init_checkpoint)
                tf.compat.v1.train.init_from_checkpoint(init_checkpoint, assignment_map)

            tf.compat.v1.logging.info("**** Trainable Variables ****")
            for var in tvars:
                init_string = ""
                if var.name in initialized_variable_names:
                    init_string = ", *INIT_FROM_CKPT*"
                tf.logging.info("  name = %s, shape = %s%s", var.name, var.shape, init_string)

            if mode == tf.estimator.ModeKeys.TRAIN:
                train_op = optimization.create_optimizer(total_loss, learning_rate, num_train_steps, num_warmup_steps, False)
                output_spec = tf.estimator.EstimatorSpec(mode=mode, loss=total_loss, train_op=train_op, )
            elif mode == tf.estimator.ModeKeys.EVAL:
                def metric_fn(per_example_loss, label_ids, logits, is_real_example):
                    predictions = tf.argmax(logits, axis=-1, output_type=tf.int32)
                    accuracy = tf.compat.v1.metrics.accuracy(
                        labels=label_ids, predictions=predictions, weights=is_real_example)
                    loss = tf.compat.v1.metrics.mean(values=per_example_loss, weights=is_real_example)
                    return {"eval_accuracy": accuracy, "eval_loss": loss, }

                metrics = metric_fn(per_example_loss, label_ids, logits, True)
                output_spec = tf.estimator.EstimatorSpec(mode=mode, loss=total_loss, eval_metric_ops=metrics)
            else:
                output_spec = tf.estimator.EstimatorSpec(mode=mode, predictions={"probabilities": probabilities}, )
            return output_spec

        return model_gpu

    def get_estimator(self):
        from tensorflow.python.estimator.estimator import Estimator
        from tensorflow.python.estimator.run_config import RunConfig

        bert_config = modeling.BertConfig.from_json_file(cf.bert_config_file)
        train_examples = self.processor.get_train_examples(cf.data_dir)
        label_list = self.processor.get_labels()       # 这里需要这样写，如果用self.get_label_list()获取列表，在还没有生成label_list.pkl文件的时候会报错
        # label_list = self.get_label_list()
        num_train_steps = int(len(train_examples) / self.batch_size * cf.num_train_epochs)
        num_warmup_steps = int(num_train_steps * 0.1)

        if self.mode == tf.estimator.ModeKeys.TRAIN:
            init_checkpoint = cf.init_checkpoint
        else:
            init_checkpoint = cf.output_dir  # 预测模式下加载

        model_fn = self.model_fn_builder(
            bert_config=bert_config,
            num_labels=len(label_list),
            init_checkpoint=init_checkpoint,
            learning_rate=cf.learning_rate,
            num_train_steps=num_train_steps,
            num_warmup_steps=num_warmup_steps,
            use_one_hot_embeddings=False)

        config = tf.compat.v1.ConfigProto()
        config.gpu_options.allow_growth = True
        config.gpu_options.per_process_gpu_memory_fraction = cf.gpu_memory_fraction
        config.log_device_placement = False

        return Estimator(model_fn=model_fn, config=RunConfig(session_config=config), model_dir=cf.output_dir, params={'batch_size': self.batch_size})

    def get_label_list(self):
        '''
        读取模型训练是动态产生的label_list.pkl文件
        :return:
        '''
        label_list = pkl.load(open('./pb/label_list.pkl', 'rb'))
        return label_list

    def predict_from_queue(self):
        for i in self.estimator.predict(input_fn=self.queue_predict_input_fn, yield_single_examples=False):
            self.output_queue.put(i)

    def queue_predict_input_fn(self):
        return (tf.data.Dataset.from_generator(
            self.generate_from_queue,
            output_types={
                'input_ids': tf.int32,
                'input_mask': tf.int32,
                'segment_ids': tf.int32,
                'label_ids': tf.int32},
            output_shapes={
                'input_ids': (None, self.max_seq_length),
                'input_mask': (None, self.max_seq_length),
                'segment_ids': (None, self.max_seq_length),
                'label_ids': (1,)}).prefetch(10))

    def generate_from_queue(self):
        while True:
            predict_examples = self.processor.get_sentence_examples(self.input_queue.get())

            features = list(self.convert_examples_to_features(predict_examples,
                                                              self.processor.get_labels(),
                                                              cf.max_seq_length,
                                                              self.tokenizer))
            yield {
                'input_ids': [f.input_ids for f in features],
                'input_mask': [f.input_mask for f in features],
                'segment_ids': [f.segment_ids for f in features],
                'label_ids': [f.label_id for f in features]
            }

    def convert_examples_to_features(self, examples, label_list, max_seq_length, tokenizer):
        """Convert a set of `InputExample`s to a list of `InputFeatures`."""

        for (ex_index, example) in enumerate(examples):
            label_map = {}
            for (i, label) in enumerate(label_list):
                label_map[label] = i

            tokens_a = tokenizer.tokenize(example.text_a)
            tokens_b = None
            if example.text_b:
                tokens_b = tokenizer.tokenize(example.text_b)

            if tokens_b:
                # Modifies `tokens_a` and `tokens_b` in place so that the total
                # length is less than the specified length.
                # Account for [CLS], [SEP], [SEP] with "- 3"
                self._truncate_seq_pair(tokens_a, tokens_b, max_seq_length - 3)
            else:
                # Account for [CLS] and [SEP] with "- 2"
                if len(tokens_a) > max_seq_length - 2:
                    tokens_a = tokens_a[0:(max_seq_length - 2)]

            tokens = []
            segment_ids = []
            tokens.append("[CLS]")
            segment_ids.append(0)
            for token in tokens_a:
                tokens.append(token)
                segment_ids.append(0)
            tokens.append("[SEP]")
            segment_ids.append(0)

            if tokens_b:
                for token in tokens_b:
                    tokens.append(token)
                    segment_ids.append(1)
                tokens.append("[SEP]")
                segment_ids.append(1)

            input_ids = tokenizer.convert_tokens_to_ids(tokens)

            # The mask has 1 for real tokens and 0 for padding tokens. Only real
            # tokens are attended to.
            input_mask = [1] * len(input_ids)

            # Zero-pad up to the sequence length.
            while len(input_ids) < max_seq_length:
                input_ids.append(0)
                input_mask.append(0)
                segment_ids.append(0)

            assert len(input_ids) == max_seq_length
            assert len(input_mask) == max_seq_length
            assert len(segment_ids) == max_seq_length

            label_id = label_map[example.label]
            if ex_index < 5:
                tf.compat.v1.logging.info("*** Example ***")
                tf.compat.v1.logging.info("guid: %s" % (example.guid))
                tf.compat.v1.logging.info("tokens: %s" % " ".join([tokenization.printable_text(x) for x in tokens]))
                tf.compat.v1.logging.info("input_ids: %s" % " ".join([str(x) for x in input_ids]))
                tf.compat.v1.logging.info("input_mask: %s" % " ".join([str(x) for x in input_mask]))
                tf.compat.v1.logging.info("segment_ids: %s" % " ".join([str(x) for x in segment_ids]))
                tf.compat.v1.logging.info("label: %s (id = %d)" % (example.label, label_id))

            feature = InputFeatures(
                input_ids=input_ids,
                input_mask=input_mask,
                segment_ids=segment_ids,
                label_id=label_id)

            yield feature

    def _truncate_seq_pair(self, tokens_a, tokens_b, max_length):
        """Truncates a sequence pair in place to the maximum length."""

        # This is a simple heuristic which will always truncate the longer sequence
        # one token at a time. This makes more sense than truncating an equal percent
        # of tokens from each, since if one sequence is very short then each token
        # that's truncated likely contains more information than a longer sequence.
        while True:
            total_length = len(tokens_a) + len(tokens_b)
            if total_length <= max_length:
                break
            if len(tokens_a) > len(tokens_b):
                tokens_a.pop()
            else:
                tokens_b.pop()

    def convert_single_example(self, ex_index, example, label_list, max_seq_length, tokenizer):
        """Converts a single `InputExample` into a single `InputFeatures`."""

        label_map = {}
        for (i, label) in enumerate(label_list):
            label_map[label] = i

        tokens_a = tokenizer.tokenize(example.text_a)
        tokens_b = None
        if example.text_b:
            tokens_b = tokenizer.tokenize(example.text_b)

        if tokens_b:
            # Modifies `tokens_a` and `tokens_b` in place so that the total
            # length is less than the specified length.
            # Account for [CLS], [SEP], [SEP] with "- 3"
            self._truncate_seq_pair(tokens_a, tokens_b, max_seq_length - 3)
        else:
            # Account for [CLS] and [SEP] with "- 2"
            if len(tokens_a) > max_seq_length - 2:
                tokens_a = tokens_a[0:(max_seq_length - 2)]

        tokens = []
        segment_ids = []
        tokens.append("[CLS]")
        segment_ids.append(0)
        for token in tokens_a:
            tokens.append(token)
            segment_ids.append(0)
        tokens.append("[SEP]")
        segment_ids.append(0)

        if tokens_b:
            for token in tokens_b:
                tokens.append(token)
                segment_ids.append(1)
            tokens.append("[SEP]")
            segment_ids.append(1)

        input_ids = tokenizer.convert_tokens_to_ids(tokens)

        # The mask has 1 for real tokens and 0 for padding tokens. Only real
        # tokens are attended to.
        input_mask = [1] * len(input_ids)

        # Zero-pad up to the sequence length.
        while len(input_ids) < max_seq_length:
            input_ids.append(0)
            input_mask.append(0)
            segment_ids.append(0)

        assert len(input_ids) == max_seq_length
        assert len(input_mask) == max_seq_length
        assert len(segment_ids) == max_seq_length

        label_id = label_map[example.label]
        if ex_index < 5:
            tf.compat.v1.logging.info("*** Example ***")
            tf.compat.v1.logging.info("guid: %s" % (example.guid))
            tf.compat.v1.logging.info("tokens: %s" % " ".join([tokenization.printable_text(x) for x in tokens]))
            tf.compat.v1.logging.info("input_ids: %s" % " ".join([str(x) for x in input_ids]))
            tf.compat.v1.logging.info("input_mask: %s" % " ".join([str(x) for x in input_mask]))
            tf.compat.v1.logging.info("segment_ids: %s" % " ".join([str(x) for x in segment_ids]))
            tf.compat.v1.logging.info("label: %s (id = %d)" % (example.label, label_id))

        feature = InputFeatures(
            input_ids=input_ids,
            input_mask=input_mask,
            segment_ids=segment_ids,
            label_id=label_id,
            is_real_example=True)
        return feature

    def file_based_convert_examples_to_features(self, examples, label_list, max_seq_length, tokenizer, output_file):
        """Convert a set of `InputExample`s to a TFRecord file."""

        writer = tf.io.TFRecordWriter(output_file)

        for (ex_index, example) in enumerate(examples):
            if ex_index % 10000 == 0:
                tf.logging.info("Writing example %d of %d" % (ex_index, len(examples)))

            feature = self.convert_single_example(ex_index, example, label_list,
                                             max_seq_length, tokenizer)

            def create_int_feature(values):
                f = tf.train.Feature(int64_list=tf.train.Int64List(value=list(values)))
                return f

            features = collections.OrderedDict()
            features["input_ids"] = create_int_feature(feature.input_ids)
            features["input_mask"] = create_int_feature(feature.input_mask)
            features["segment_ids"] = create_int_feature(feature.segment_ids)
            features["label_ids"] = create_int_feature([feature.label_id])
            features["is_real_example"] = create_int_feature(
                [int(feature.is_real_example)])

            tf_example = tf.train.Example(features=tf.train.Features(feature=features))
            writer.write(tf_example.SerializeToString())
        writer.close()


    def file_based_input_fn_builder(self, input_file, seq_length, is_training,
                                    drop_remainder):
        """Creates an `input_fn` closure to be passed to TPUEstimator."""

        name_to_features = {
            "input_ids": tf.io.FixedLenFeature([seq_length], tf.int64),
            "input_mask": tf.io.FixedLenFeature([seq_length], tf.int64),
            "segment_ids": tf.io.FixedLenFeature([seq_length], tf.int64),
            "label_ids": tf.io.FixedLenFeature([], tf.int64),
            "is_real_example": tf.io.FixedLenFeature([], tf.int64),
        }

        def _decode_record(record, name_to_features):
            """Decodes a record to a TensorFlow example."""
            example = tf.parse_single_example(record, name_to_features)

            # tf.Example only supports tf.int64, but the TPU only supports tf.int32.
            # So cast all int64 to int32.
            for name in list(example.keys()):
                t = example[name]
                if t.dtype == tf.int64:
                    t = tf.to_int32(t)
                example[name] = t

            return example

        def input_fn(params):
            """The actual input function."""
            batch_size = params["batch_size"]

            # For training, we want a lot of parallel reading and shuffling.
            # For eval, we want no shuffling and parallel reading doesn't matter.
            d = tf.data.TFRecordDataset(input_file)
            if is_training:
                d = d.repeat()
                d = d.shuffle(buffer_size=100)

            d = d.apply(
                tf.data.experimental.map_and_batch(
                    lambda record: _decode_record(record, name_to_features),
                    batch_size=batch_size,
                    drop_remainder=drop_remainder))
            return d

        return input_fn

    # This function is not used by this file but is still used by the Colab and people who depend on it.
    def input_fn_builder(self, features, seq_length, is_training, drop_remainder):
        """Creates an `input_fn` closure to be passed to TPUEstimator."""

        all_input_ids = []
        all_input_mask = []
        all_segment_ids = []
        all_label_ids = []

        for feature in features:
            all_input_ids.append(feature.input_ids)
            all_input_mask.append(feature.input_mask)
            all_segment_ids.append(feature.segment_ids)
            all_label_ids.append(feature.label_id)

        def input_fn(params):
            """The actual input function."""
            batch_size = params["batch_size"]

            num_examples = len(features)
            # This is for demo purposes and does NOT scale to large data sets. We do
            # not use Dataset.from_generator() because that uses tf.py_func which is
            # not TPU compatible. The right way to load data is with TFRecordReader.
            d = tf.data.Dataset.from_tensor_slices({
                "input_ids":
                    tf.constant(all_input_ids, shape=[num_examples, seq_length],
                                dtype=tf.int32),
                "input_mask":
                    tf.constant(all_input_mask, shape=[num_examples, seq_length],
                                dtype=tf.int32),
                "segment_ids":
                    tf.constant(all_segment_ids, shape=[num_examples, seq_length],
                                dtype=tf.int32),
                "label_ids":
                    tf.constant(all_label_ids, shape=[num_examples], dtype=tf.int32),
            })

            if is_training:
                d = d.repeat()
                d = d.shuffle(buffer_size=100)

            d = d.batch(batch_size=batch_size, drop_remainder=drop_remainder)
            return d

        return input_fn


    def create_classification_model(self, bert_config, is_training, input_ids, input_mask, segment_ids, labels, num_labels):
        # 通过传入的训练数据，进行representation
        model = modeling.BertModel(
            config=bert_config,
            is_training=is_training,
            input_ids=input_ids,
            input_mask=input_mask,
            token_type_ids=segment_ids,
        )

        embedding_layer = model.get_sequence_output()
        output_layer = model.get_pooled_output()
        hidden_size = output_layer.shape[-1].value

        output_weights = tf.get_variable(
            "output_weights", [num_labels, hidden_size],
            initializer=tf.truncated_normal_initializer(stddev=0.02))

        output_bias = tf.get_variable(
            "output_bias", [num_labels], initializer=tf.zeros_initializer())

        with tf.variable_scope("loss"):
            if is_training:
                # I.e., 0.1 dropout
                output_layer = tf.nn.dropout(output_layer, keep_prob=0.9)

            logits = tf.matmul(output_layer, output_weights, transpose_b=True)
            logits = tf.nn.bias_add(logits, output_bias)
            probabilities = tf.nn.softmax(logits, axis=-1)
            log_probs = tf.nn.log_softmax(logits, axis=-1)

            if labels is not None:
                one_hot_labels = tf.one_hot(labels, depth=num_labels, dtype=tf.float32)

                per_example_loss = -tf.reduce_sum(one_hot_labels * log_probs, axis=-1)
                loss = tf.reduce_mean(per_example_loss)
            else:
                loss, per_example_loss = None, None
        return (loss, per_example_loss, logits, probabilities)


    def save_PBmodel(self, num_labels):
        """    保存PB格式中文分类模型    """
        try:
            # 如果PB文件已经存在，则返回PB文件的路径，否则将模型转化为PB文件，并且返回存储PB文件的路径
            pb_file = os.path.join(cf.pb_model_dir, 'classification_model.pb')
            graph = tf.Graph()
            with graph.as_default():
                input_ids = tf.placeholder(tf.int32, (None, cf.max_seq_length), 'input_ids')
                input_mask = tf.placeholder(tf.int32, (None, cf.max_seq_length), 'input_mask')
                bert_config = modeling.BertConfig.from_json_file(cf.bert_config_file)
                loss, per_example_loss, logits, probabilities = self.create_classification_model(
                    bert_config=bert_config,
                    is_training=False,
                    input_ids=input_ids,
                    input_mask=input_mask,
                    segment_ids=None,
                    labels=None,
                    num_labels=num_labels)

                probabilities = tf.identity(probabilities, 'pred_prob')
                saver = tf.train.Saver()

                with tf.Session() as sess:
                    sess.run(tf.global_variables_initializer())
                    latest_checkpoint = tf.train.latest_checkpoint(cf.output_dir)
                    saver.restore(sess, latest_checkpoint)
                    tmp_g = tf.compat.v1.graph_util.convert_variables_to_constants(sess, graph.as_graph_def(), ['pred_prob'])

            # 存储二进制模型到文件中
            with tf.gfile.GFile(pb_file, 'wb') as f:
                f.write(tmp_g.SerializeToString())
            return pb_file
        except Exception as e:
            print('fail to optimize the graph! %s', e)


    def train(self):
        '''
        domain 模型训练
        :return:
        '''
        if self.mode is None:
            raise ValueError("Please set the 'mode' parameter")

        bert_config = modeling.BertConfig.from_json_file(cf.bert_config_file)

        if cf.max_seq_length > bert_config.max_position_embeddings:
            raise ValueError(
                "Cannot use sequence length %d because the BERT model "
                "was only trained up to sequence length %d" %
                (cf.max_seq_length, bert_config.max_position_embeddings))

        tf.gfile.MakeDirs(cf.output_dir)

        train_examples = self.processor.get_train_examples(cf.data_dir)
        label_list = self.processor.get_labels()       # 从训练数据中动态获取label标签， 并且将其映射成pkl文件
        label_map = {}
        for (i, label) in enumerate(label_list):
            label_map[label] = i
        with open('label_list.pkl', 'wb') as f:
            pickle.dump(label_list, f)
        with open('label2id.pkl', 'wb') as f:
            pickle.dump(label_map, f)

        num_train_steps = int(len(train_examples) / cf.batch_size * cf.num_train_epochs)

        estimator = self.get_estimator()

        train_file = os.path.join(cf.output_dir, "train.tf_record")
        self.file_based_convert_examples_to_features(train_examples, label_list, cf.max_seq_length, self.tokenizer,
                                                     train_file)
        tf.compat.v1.logging.info("***** Running training *****")
        tf.compat.v1.logging.info("  Num examples = %d", len(train_examples))
        tf.compat.v1.logging.info("  Batch size = %d", cf.batch_size)
        tf.compat.v1.logging.info("  Num steps = %d", num_train_steps)
        train_input_fn = self.file_based_input_fn_builder(input_file=train_file,
                                                          seq_length=cf.max_seq_length,
                                                          is_training=True,
                                                          drop_remainder=True)

        # early_stopping = tf.contrib.estimator.stop_if_no_decrease_hook(
        #     estimator,
        #     metric_name='loss',
        #     max_steps_without_decrease=10,
        #     min_steps=num_train_steps)

        # estimator.train(input_fn=train_input_fn, hooks=[early_stopping])
        estimator.train(input_fn=train_input_fn, max_steps=num_train_steps)

    def eval(self):
        if self.mode is None:
            raise ValueError("Please set the 'mode' parameter")
        eval_examples = self.processor.get_dev_examples(cf.data_dir)
        eval_file = os.path.join(cf.output_dir, "eval.tf_record")
        label_list = self.processor.get_labels()
        self.file_based_convert_examples_to_features(
            eval_examples, label_list, cf.max_seq_length, self.tokenizer, eval_file)

        tf.compat.v1.logging.info("***** Running evaluation *****")
        tf.compat.v1.logging.info("  Num examples = %d", len(eval_examples))
        tf.compat.v1.logging.info("  Batch size = %d", self.batch_size)

        eval_input_fn = self.file_based_input_fn_builder(
            input_file=eval_file,
            seq_length=cf.max_seq_length,
            is_training=False,
            drop_remainder=False)

        estimator = self.get_estimator()
        result = estimator.evaluate(input_fn=eval_input_fn, steps=None)

        output_eval_file = os.path.join(cf.output_dir, "eval_results.txt")
        with tf.gfile.GFile(output_eval_file, "w") as writer:
            tf.compat.v1.logging.info("***** Eval results *****")
            for key in sorted(result.keys()):
                tf.compat.v1.logging.info("  %s = %s", key, str(result[key]))
                writer.write("%s = %s\n" % (key, str(result[key])))

    def predict(self, sentence):
        '''
        domain 分类模型预测
        :param sentence:
        :return:
        '''
        if self.mode is None:
            raise ValueError("Please set the 'mode' parameter")
        self.input_queue.put([sentence])
        label = self.get_label_list()
        prediction = label[int(np.argmax(self.output_queue.get()['probabilities']))]
        return prediction


    # save_PBmodel(len(label_list))  # 生成单个pb模型。
if __name__ == '__main__':
    import time
    cls = DomainCLS()
    cls.set_mode(tf.estimator.ModeKeys.PREDICT)
    testcase = ['中国高铁动车组通常运行速度是多少？', '中国高铁动车组通常运行速度是多少？', '梨树种下之后，过几年能结果子？', '青蛙在吃虫子时眼睛是怎样的状态？']
    for sentence in testcase:
        atime = time.clock()
        y = cls.predict(sentence)
        btime=time.clock()
        print(y)
        print(btime-atime)